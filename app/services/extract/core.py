"""Small, evidence-first extraction pipeline: catalog, one call per product, derived ICP."""
import json
import re
from decimal import Decimal
from urllib.parse import urlsplit
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.entities import Company, FieldClaim, Product, PricePlan, Source
from app.services.common import add_claim, domain, field_meta_set, safe_name
from app.services.llm import json_completion
from app.services.rag.core import retrieve
from app.services.extract.fallback import (feature_fallback, use_case_fallback, positioning_fallback,
    pricing_model_fallback, market_segment_fallback)


CATALOG_KINDS = {'standalone_product','suite','module','feature','service','integration','unknown'}
PRODUCT_FIELDS = {'description','main_use_case','category','product_type','positioning',
                  'business_model','key_features','value_proposition','icp','target_market'}


def exact_source(quote: str | None, source_url: str | None, sources: list[Source]) -> Source | None:
    """Only accept verifiable quotes, with safe fallback for missing source_url."""
    if not isinstance(quote, str) or len(quote.strip()) < 12:
        return None
    matches = [source for source in sources if quote.casefold() in (source.text or '').casefold()]
    if source_url:
        # Prefer the matching page. Do not cite an unrelated company or a sibling product.
        path = source_url.strip().rstrip('/').lower()
        named = [source for source in matches if source.url.rstrip('/').lower() == path]
        if named:
            return named[0]
        return None
    # A missing source URL is permissible only for an exact quote found in one source.
    return matches[0] if len(matches) == 1 else None


def sample_catalog(pages: list[Source], company: Company) -> str:
    lines = [f'Company={company.name} Website={company.website_url}']
    for s in sorted(pages, key=lambda x: 0 if x.page_type in ('home','product','about') else 1)[:12]:
        structured = s.structured or {}
        heading = structured.get('h1','')
        lines.append(f'URL={s.url}\nTITLE={structured.get("title", "")}\nH1={heading}\n'
                     f'NAV={json.dumps(structured.get("nav", [])[:40])[:1000]}\n'
                     f'JSONLD={json.dumps(structured.get("json_ld", []))[:1600]}')
        # Only explicit product headers/nearby text from about pages, not whole 150K text.
        if s.page_type in {'about','product'} and re.search(r'(?i)our products|our offerings|products include', s.text):
            pos = re.search(r'(?i)our products|our offerings|products include', s.text).start()
            lines.append('PRODUCT_SECTION=' + s.text[pos:pos+1000])
    return '\n'.join(lines)[:16_000]


def fallback_catalog(sources: list[Source], company: Company) -> list[dict]:
    """Conservative fallback, never hallucinate children from blog/FAQ pages."""
    found: list[dict] = []
    for s in sources:
        if s.page_type != 'product':
            continue
        path = [x for x in urlsplit(s.url).path.split('/') if x]
        if len(path) < 2 or path[0] not in ('products','product'):
            continue
        title = (s.structured or {}).get('h1') or (s.structured or {}).get('title') or ''
        title = re.split(r' \|| — | - ', title)[0].strip()
        if safe_name(title) and any(title.casefold() in x.text.casefold() for x in sources):
            found.append({'name':title, 'kind':'standalone_product', 'url':s.url, 'quote':title})
    # Explicit first-party "Our Products" section: conservative short heading detection.
    for s in sources:
        section = re.search(r'(?i)\bour products\b|\bour offerings\b', s.text or '')
        if not section or s.page_type not in ('home','about','product'):
            continue
        lines = [x.strip() for x in s.text[section.end():section.end()+1600].splitlines() if x.strip()]
        for line in lines[:20]:
            label = re.split(r'\s+[-–—:]\s+|\s*[|]\s*', line)[0].strip()
            if not (3 <= len(label) <= 35 and len(label.split()) <= 3 and
                    re.fullmatch(r'[A-Z][\w.]+(?:\s+[A-Z][\w.]+){0,2}', label)):
                continue
            if label.lower() in {'our products','our offerings','learn more','get started','contact us'}:
                continue
            if any(x['name'].lower() == label.lower() for x in found):
                continue
            found.append({'name': label, 'kind': 'standalone_product', 'url': s.url,
                          'quote': label, 'source_url': s.url})
    if not found:
        home = next((s for s in sources if s.page_type == 'home'), sources[0] if sources else None)
        if home:
            site_name = (home.structured or {}).get('metadata',{}).get('og:site_name')
            brand = str(site_name or re.split(r'\s+[|–—-]\s+',
                    (home.structured or {}).get('title') or '')[0] or
                    (company.domain or company.name).split('.')[0]).strip()[:100]
            if safe_name(brand) and brand.casefold() in home.text.casefold():
                found.append({'name':brand, 'kind':'standalone_product',
                              'url':home.url, 'quote':brand, 'source_url':home.url})
    return found[:12]


async def catalog(db: AsyncSession, company: Company, sources: list[Source]) -> list[Product]:
    if not sources:
        return []
    context = sample_catalog(sources, company)
    raw = await json_completion('You are a strict product catalog extractor. Output only JSON. '
        'A product is a named marketable offering; do not create products from plans, FAQ, '
        'tools, generic site sections or competitor examples. A company-branded SaaS may '
        'be a sellable standalone product. Modules belong under their parent. '
        'Use each entity\'s exact name and an exact quote from the source, plus source URL.',
        'Return {"products":[{"name":"...","kind":"standalone_product|suite|module|feature|service|unknown",'
        '"url":"...","source_url":"...","quote":"...","parent_name":null}]}.\n' + context)
    candidates = raw.get('products') if isinstance(raw.get('products'), list) else None
    deterministic = fallback_catalog(sources, company)
    if not candidates:
        candidates = deterministic
    elif len(candidates) < len(deterministic):
        existing = {str(v.get('name','')).casefold() for v in candidates if isinstance(v,dict)}
        candidates += [v for v in deterministic if v['name'].casefold() not in existing]
    accepted = []
    for item in candidates[:15]:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        kind = item.get('kind') if item.get('kind') in CATALOG_KINDS else 'unknown'
        if not safe_name(name) or kind in {'feature','integration'}:
            continue
        source = exact_source(item.get('quote'), item.get('source_url') or item.get('url'), sources)
        if source is None:
            # Fallback candidates have deterministic title+URL evidence, but still need exact text.
            source = next((s for s in sources if name.casefold() in s.text.casefold()), None)
        if not source:
            continue
        product = await db.scalar(select(Product).where(Product.company_id == company.id,
                                                          Product.name == name))
        if not product:
            product = Product(company_id=company.id, name=name, website_url=item.get('url'),
                              candidate_kind=kind, status='detected')
            db.add(product)
            await db.flush()
        else:
            if product.status != 'verified':
                product.website_url = product.website_url or item.get('url')
        await add_claim(db, company.id, product.id, 'name', name, source,
                        item.get('quote') if source.text.lower().find(str(item.get('quote') or '').lower()) >= 0 else name)
        accepted.append((product, item))
    by_name = {p.name.casefold(): p for p, _ in accepted}
    for product, item in accepted:
        parent = by_name.get(str(item.get('parent_name') or '').casefold())
        if parent and parent.id != product.id and product.status != 'verified':
            product.parent_id = parent.id
    await db.flush()
    return [p for p,_ in accepted]


def product_sources(product: Product, sources: list[Source], count: int) -> list[Source]:
    """Prioritize the product's identity pages; avoid 6 unrelated app-use-case pages."""
    slug = re.sub(r'[^a-z0-9]+', '-', product.name.casefold()).strip('-')
    def matches_product(source):
        return ((slug and slug in urlsplit(source.url).path.casefold()) or
                product.name.casefold() in ((source.structured or {}).get('h1', '')).casefold())
    strong = [s for s in sources if s.page_type in ('product', 'home', 'features', 'about')
              and matches_product(s)]
    if count == 1:
        core = [s for s in sources if s.page_type in ('home', 'features', 'product', 'about', 'pricing')]
        core.sort(key=lambda s: {'home': 0, 'features': 1, 'product': 2, 'pricing': 3, 'about': 4}.get(s.page_type, 10))
        return core[:8]
    matched = [s for s in sources if s.page_type in ('product', 'features', 'home', 'about')
               and product.name.casefold() in (s.text or '').casefold()]
    results = []
    for source in strong + matched:
        if source not in results:
            results.append(source)
    return results[:6]


def _candidate_value(raw, key: str):
    value = raw.get(key)
    if isinstance(value, dict) and 'value' in value:
        return value['value'], value.get('quote'), value.get('source_url')
    return value, None, None


async def extract_products(db: AsyncSession, company: Company, products: list[Product], sources: list[Source]):
    for product in products:
        relevant = product_sources(product, sources, len(products))
        if not relevant:
            continue
        allowed_ids = [s.id for s in relevant]
        retrieval = await retrieve(db, company.id, f'{product.name} key features capabilities use cases',
                                   source_ids=allowed_ids, k=7)
        facts = '\n\n'.join(f'URL={next((s.url for s in relevant if s.id == c["source_id"]), "")}\n'
                           f'TEXT={c["text"]}' for c in retrieval)[:7500]
        # Include key pages even when FTS/vector retrieval produces poor or zero matches.
        # Otherwise product-specific features disappear despite being crawled successfully.
        overview = '\n\n'.join(f'URL={s.url}\nPAGE_TYPE={s.page_type}\nTEXT={s.text[:1400]}'
                               for s in relevant[:5])[:7000]
        prompt = (f'PRODUCT={product.name}\nPRIMARY PAGES:\n{overview}\nRETRIEVED:\n{facts}'
                  '\nReturn JSON with description, category, product_type, main_use_case, positioning, '
                  'business_model, key_features. For every scalar field return '
                  '{"value": ..., "quote": "exact excerpt", "source_url": "URL"}. '
                  'For key_features return an array of {"value", "quote", "source_url"}. '
                  'Do not skip key_features when the /features page contains descriptive capabilities. '
                  'Use null when unsupported. Never invent a source or quote.')
        raw = await json_completion('Extract facts for THIS product only. Never mix sibling products. '
            'Every quote must be a literal substring of the specific URL given. JSON only.', prompt)
        meta = dict(product.field_meta or {})
        for field in ('description', 'category', 'product_type', 'main_use_case', 'positioning',
                      'business_model', 'key_features'):
            if meta.get(field, {}).get('review') == 'accepted':
                continue
            value, quote, url = _candidate_value(raw, field)
            if field == 'key_features':
                values, citations = [], []
                for entry in value if isinstance(value, list) else []:
                    if isinstance(entry, str):
                        # A plain list is valid only if the item appears verbatim on a source.
                        item_value, item_quote, item_url = entry.strip(), entry.strip(), None
                    elif isinstance(entry, dict):
                        item_value = entry.get('value') or entry.get('name')
                        item_quote, item_url = entry.get('quote'), entry.get('source_url')
                    else:
                        continue
                    src = exact_source(item_quote, item_url, relevant)
                    if src and isinstance(item_value, str) and item_value.strip():
                        values.append(item_value.strip()[:220])
                        citations.append((item_value, src, item_quote))
                if values:
                    product.key_features = list(dict.fromkeys(values))[:12]
                    meta = field_meta_set(meta, field, status='found_on_site', review='pending',
                                          confidence=75, source_id=citations[0][1].id,
                                          quote=citations[0][2])
                    for value_, src, quote_ in citations[:12]:
                        await add_claim(db, company.id, product.id, field, value_, src, quote_)
                continue
            source = exact_source(quote, url, relevant)
            if source and value not in (None, '', [], {}):
                if field == 'business_model':
                    value = value if isinstance(value, dict) else {'summary': str(value)}
                setattr(product, field, value)
                meta = field_meta_set(meta, field, status='found_on_site', review='pending',
                                      confidence=75, source_id=source.id, quote=quote)
                await add_claim(db, company.id, product.id, field, value, source, quote)
        # Strict source-linked fallback for unavailable/malformed LLM output.
        if meta.get('description', {}).get('review') != 'accepted' and not product.description:
            desc = next((x for x in relevant if x.page_type in ('home', 'product') and x.text), relevant[0])
            sentence = next((q for q in desc.text.splitlines() if 45 <= len(q.strip()) <= 260
                             and product.name.casefold() in q.casefold()), None)
            if sentence:
                quote = sentence.strip()
                product.description = quote
                meta = field_meta_set(meta, 'description', status='found_on_site', review='pending',
                                      confidence=65, source_id=desc.id, quote=quote)
                await add_claim(db, company.id, product.id, 'description', quote, desc, quote)
        if meta.get('key_features', {}).get('review') != 'accepted' and not product.key_features:
            fallback = feature_fallback(relevant)
            if fallback:
                product.key_features = [item['value'] for item in fallback]
                meta = field_meta_set(meta, 'key_features', status='found_on_site', review='pending',
                                      confidence=60, source_id=fallback[0]['source'].id,
                                      quote=fallback[0]['quote'])
                for item in fallback:
                    await add_claim(db, company.id, product.id, 'key_features', item['value'],
                                    item['source'], item['quote'])
        for field, getter in (('main_use_case', use_case_fallback), ('positioning', positioning_fallback)):
            if meta.get(field, {}).get('review') == 'accepted' or getattr(product, field):
                continue
            fact = getter(relevant)
            if fact:
                setattr(product, field, fact['value'])
                meta = field_meta_set(meta, field, status='found_on_site', review='pending',
                                      confidence=60, source_id=fact['source'].id, quote=fact['quote'])
                await add_claim(db, company.id, product.id, field, fact['value'],
                                fact['source'], fact['quote'])
        if meta.get('business_model', {}).get('review') != 'accepted' and not product.business_model:
            # Never attach a shared pricing page to multiple unrelated products.
            sellable = [p for p in products if p.candidate_kind in ('suite', 'standalone_product')]
            if len(sellable) == 1:
                fact = pricing_model_fallback(relevant)
                if fact:
                    product.business_model = fact['value']
                    meta = field_meta_set(meta, 'business_model', status='ai_suggested', review='pending',
                                          confidence=55, source_id=fact['source'].id, quote=fact['quote'])
                    await add_claim(db, company.id, product.id, 'business_model', fact['value'],
                                    fact['source'], fact['quote'], status='ai_suggested')
        product.field_meta = meta
        await db.flush()


PRICE = re.compile(r'(?P<cur>\$|USD\s*|€|£|₹)\s*(?P<n>\d+(?:[.,]\d{1,2})?)\s*'
                   r'(?P<period>/(?:agent|seat|user|month|year|mo|yr|outcome|conversation)|per\s+(?:agent|seat|user|month|year|outcome))?', re.I)


def pricing_lines(text: str) -> list[dict]:
    """Extract only explicit plan/amount line pairs; mark other prices uncertain."""
    result=[]
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    for i,line in enumerate(lines):
        match=PRICE.search(line)
        if not match:
            continue
        window=' '.join(lines[max(0,i-2):i+1])[:320]
        if re.search(r'(?i)competitor|vs\.|example only|case study|comparison', window):
            continue
        # Plan title must be clearly present in same or immediately preceding heading.
        possible=lines[i-1] if i else ''
        label=re.split(r'[|•]', line[:match.start()])[0].strip() or possible
        if not (2<=len(label)<=60 and re.search(r'[A-Za-z]',label)):
            continue
        value=match.group('n').replace(',','')
        cur={'$':'USD','€':'EUR','£':'GBP','₹':'INR'}.get(match.group('cur').strip(),'USD')
        result.append({'name':label,'amount':value,'currency':cur,'unit':match.group('period'),
                       'quote':line[:400]})
    return result[:12]


async def extract_pricing(db: AsyncSession, company: Company, products: list[Product], sources: list[Source]):
    for product in products:
        if product.status == 'verified' or product.field_meta.get('pricing_status',{}).get('review')=='accepted':
            continue
        slug=re.sub(r'[^a-z0-9]+','-',product.name.lower()).strip('-')
        pricing=[s for s in sources if s.page_type == 'pricing' and
                 slug and slug in urlsplit(s.url).path.lower()]
        # A generic /pricing page is unambiguous only when there is one sellable product.
        sellable=[p for p in products if p.candidate_kind in {'suite','standalone_product'}]
        if not pricing and len(sellable)==1 and sellable[0].id==product.id:
            pricing=[s for s in sources if s.page_type=='pricing']
        if not pricing:
            continue
        seen=set()
        for source in pricing[:3]:
            for item in pricing_lines(source.text):
                key=(item['name'].lower(),item['amount'],item['unit'])
                if key in seen:
                    continue
                seen.add(key)
                if product.id and await db.scalar(select(PricePlan.id).where(
                    PricePlan.product_id==product.id, PricePlan.name==item['name'],
                    PricePlan.source_id==source.id).limit(1)):
                    continue
                db.add(PricePlan(company_id=company.id,product_id=product.id,source_id=source.id,
                                 name=item['name'],price_amount=Decimal(item['amount']),currency=item['currency'],
                                 unit=item['unit'],period='annual_or_monthly_unconfirmed',quote=item['quote']))
                await add_claim(db,company.id,product.id,'pricing',item,source,item['quote'])
        if seen:
            product.pricing_status='published_needs_review'
        elif any('contact sales' in s.text.lower() or 'request pricing' in s.text.lower() for s in pricing):
            product.pricing_status='custom'
        product.field_meta = field_meta_set(product.field_meta,'pricing_status',confidence=60,
                                           status='found_on_site',review='pending')


async def derive_company(db: AsyncSession, company: Company, sources: list[Source]):
    if not sources:
        return
    ordered = sorted((s for s in sources if s.page_type in ('home', 'about', 'product', 'features', 'pricing')),
                     key=lambda s: {'home': 0, 'about': 1, 'pricing': 2, 'product': 3, 'features': 4}.get(s.page_type, 10))
    context = '\n\n'.join(f'URL={s.url}\nTYPE={s.page_type}\nTEXT={s.text[:1600]}'
                           for s in ordered[:6])[:8500]
    raw = await json_completion("Derive company GTM suggestions ONLY from the given company\'s first-party "
        'evidence; return JSON. Never invent numbers, company sizes, regions, prices or customer counts. '
        'Derived values remain ai_suggested, never verified.',
        'Return JSON with "industry" (string/null), "business_model" (object), '
        '"market_segment" (object), "value_proposition" (object), "icp" (object). '
        'Do not return empty objects when evidence supports clear fields.\n' + context)
    meta = dict(company.field_meta or {})
    for field in ('industry', 'business_model', 'market_segment', 'value_proposition', 'icp'):
        value = raw.get(field)
        if value in (None, '', [], {}) or meta.get(field, {}).get('review') == 'accepted' or meta.get(field, {}).get('status') == 'found_on_site':
            continue
        if field != 'industry' and not isinstance(value, dict):
            continue
        setattr(company, field, value)
        meta = field_meta_set(meta, field, status='ai_suggested', review='pending', confidence=65)
        await add_claim(db, company.id, None, field, value, None, None, status='ai_suggested')
    # When LLM is unreachable, use evidence-backed suggestions rather than silently empty {}.
    fallback = {
        'business_model': pricing_model_fallback(ordered),
        'market_segment': market_segment_fallback(ordered),
        'value_proposition': positioning_fallback(ordered),
    }
    if fallback['value_proposition']:
        item = fallback['value_proposition']
        fallback['value_proposition'] = {'value': {'summary': item['value']},
                                         'source': item['source'], 'quote': item['quote']}
    for field, item in fallback.items():
        if (not item or getattr(company, field) or
                meta.get(field, {}).get('review') == 'accepted'):
            continue
        company_value = item['value']
        setattr(company, field, company_value)
        meta = field_meta_set(meta, field, status='ai_suggested', review='pending',
                              confidence=55, source_id=item['source'].id, quote=item['quote'])
        await add_claim(db, company.id, None, field, company_value,
                        item['source'], item['quote'], status='ai_suggested')
    if not company.icp and company.market_segment and meta.get('icp', {}).get('review') != 'accepted':
        audience = company.market_segment.get('summary')
        if audience:
            company.icp = {'target_audience': audience}
            meta = field_meta_set(meta, 'icp', status='ai_suggested', review='pending', confidence=50,
                                  source_id=meta.get('market_segment', {}).get('source_id'),
                                  quote=meta.get('market_segment', {}).get('quote'))
            src = next((s for s in ordered if s.id == meta['icp']['source_id']), None)
            await add_claim(db, company.id, None, 'icp', company.icp, src,
                            meta['icp'].get('quote'), status='ai_suggested')
    company.field_meta = meta


async def extract_company_structured(db: AsyncSession, company: Company, sources: list[Source]):
    """Deterministic first-party Organization JSON-LD and metadata, with conflict history."""
    for source in sources:
        if source.kind != 'website' or source.page_type not in {'home','about'}:
            continue
        structured = source.structured or {}
        items = list(structured.get('json_ld',[]))
        nodes=[]
        for item in items:
            if isinstance(item,dict):
                nodes.extend(item.get('@graph',[]) if isinstance(item.get('@graph'),list) else [item])
            elif isinstance(item,list):
                nodes.extend(item)
        for node in nodes:
            if not isinstance(node,dict):
                continue
            types=node.get('@type',[])
            types=[types] if isinstance(types,str) else types
            if not any(t in {'Organization','Corporation'} for t in types):
                continue
            identity=str(node.get('name') or '')
            if identity and company.domain and company.domain.split('.')[0].lower() not in identity.lower():
                continue
            attrs={'description':node.get('description'),'founded_year':node.get('foundingDate')}
            addr=node.get('address')
            if isinstance(addr,dict):
                attrs['headquarters']=addr.get('addressLocality')
                attrs['country']=addr.get('addressCountry')
            for field,value in attrs.items():
                if value in (None,'',[],{}):
                    continue
                if field=='founded_year':
                    match=re.match(r'^(19|20)\d{2}',str(value))
                    if not match:
                        continue
                    value=int(match.group(0))
                field_meta=(company.field_meta or {}).get(field,{})
                if field_meta.get('review')=='accepted':
                    continue
                # Exact field is in preserved structured data, not necessarily rendered text.
                quote=str(value)
                await add_claim(db,company.id,None,field,value,source,quote,status='structured')
                current=getattr(company,field)
                if current not in (None,'',[],{}) and current!=value:
                    meta=dict(company.field_meta or {}); one=dict(meta.get(field,{}))
                    one['conflict']=True
                    one['review']='pending'
                    meta[field]=one;company.field_meta=meta
                else:
                    setattr(company,field,value)
                    company.field_meta=field_meta_set(company.field_meta,field,
                        status='found_on_site',review='pending',confidence=90,
                        source_id=source.id,quote=quote)
        if not company.description:
            desc=structured.get('metadata',{}).get('description') or structured.get('metadata',{}).get('og:description')
            if desc and len(desc)>25:
                company.description=desc[:1000]
                company.field_meta=field_meta_set(company.field_meta,'description',status='found_on_site',
                                                   review='pending',confidence=85,source_id=source.id,quote=desc)
                await add_claim(db,company.id,None,'description',desc,source,desc,status='structured')
        links=structured.get('nav',[])
        social={}
        for item in links:
            url=item.get('url','') if isinstance(item,dict) else ''
            for site in ('linkedin.com','youtube.com','instagram.com','x.com','twitter.com','github.com'):
                if site in url and site not in social:
                    social[site]=url
        if social:
            company.socials={**(company.socials or {}),**social}
