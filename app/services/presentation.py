"""Maps stored product/company data onto the exact field names the frontend binds to.

The DB keeps evidence-shaped data: `icp`/`target_market`/`value_proposition` are
free-form JSONB whose keys vary by whichever extractor filled them (the local AI
pass, the GTM crawler's business profile, or a user edit), `key_features` is a
flat list, and pricing lives in its own `pricing_items` table. The verification
UI binds to fixed names instead (`icp.targetAudience`, `target_market.segment`,
`pricing.plans[].billingPeriod`, ...), so every accessor here reads through a
list of known aliases rather than one hard-coded key — that is what stops
correctly-extracted data from showing as "Not provided" just because the
extractor named it `target_audience` instead of `targetAudience`.

Empty values resolve to "" / [] rather than null, so the UI never has to
null-check a field; `status` and `confidence` on each section say whether the
blank is "not found yet" or genuinely absent.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.services.common import field_status

CURRENCY_SYMBOL = {'USD': '$', 'EUR': '€', 'GBP': '£', 'INR': '₹'}

# Best-known state first; _section() reports the strongest across its columns.
_STATUS_RANK = {'verified': 0, 'extracted': 1, 'needs_confirmation': 2, 'inferred': 3,
                'not_found': 4}


def _text(value, default: str = '') -> str:
    """Any JSONB leaf -> one readable line. Never returns None."""
    if value in (None, '', [], {}):
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, list):
        parts = [_text(v) for v in value]
        return ', '.join(p for p in parts if p) or default
    if isinstance(value, dict):
        parts = [_text(v) for v in value.values()]
        return ', '.join(p for p in parts if p) or default
    return str(value)


def _list(value) -> list[str]:
    """Any JSONB leaf -> list of display strings. Never returns None."""
    if value in (None, '', [], {}):
        return []
    if isinstance(value, str):
        # A comma/newline joined string is a list the extractor flattened.
        parts = [p.strip() for p in re.split(r'[\n;]|,(?![^(]*\))', value) if p.strip()]
        return parts if len(parts) > 1 else [value.strip()]
    if isinstance(value, list):
        out = []
        for item in value:
            text = _text(item)
            if text:
                out.append(text)
        return out
    if isinstance(value, dict):
        return [t for t in (_text(v) for v in value.values()) if t]
    return [str(value)]


def _pick(source, *aliases):
    """First alias present and non-empty in a JSONB dict, matched case- and
    separator-insensitively so `target_audience`, `targetAudience` and
    `Target Audience` all resolve to the same value."""
    if not isinstance(source, dict):
        return None
    normalized = {re.sub(r'[^a-z0-9]', '', str(k).lower()): v for k, v in source.items()}
    for alias in aliases:
        key = re.sub(r'[^a-z0-9]', '', alias.lower())
        value = normalized.get(key)
        if value not in (None, '', [], {}):
            return value
    return None


def _consume(source, used: set, *aliases):
    """_pick(), but records which original key matched so _additional() can
    return everything the fixed UI names did not claim."""
    if not isinstance(source, dict):
        return None
    for key, value in source.items():
        flat = re.sub(r'[^a-z0-9]', '', str(key).lower())
        if value in (None, '', [], {}):
            continue
        for alias in aliases:
            if flat == re.sub(r'[^a-z0-9]', '', alias.lower()):
                used.add(key)
                return value
    return None


def _additional(source, used: set) -> dict:
    """Extractor keys with no fixed slot in the card — kept so richer AI/crawler
    output (compliance_needs, voice_call_volume, ...) is shown rather than lost."""
    if not isinstance(source, dict):
        return {}
    return {key: _text(value) for key, value in source.items()
            if key not in used and value not in (None, '', [], {})}


STATUS_CONFIDENCE = {'verified': 100, 'extracted': 75, 'needs_confirmation': 50,
                     'inferred': 50, 'not_found': 0}


def _confidence(meta: dict | None, field: str, status: str | None = None) -> int:
    """The tracked confidence, or the one its status implies when extraction
    never recorded an entry — so a populated field never reads as 0%."""
    entry = (meta or {}).get(field) or {}
    score = entry.get('confidence')
    if isinstance(score, (int, float)):
        return int(score)
    return STATUS_CONFIDENCE.get(status or '', 0)


def _section(product, meta: dict | None, field: str, *extra_fields: str) -> dict:
    """status + confidence header carried by every card section. A card backed
    by several columns (Overview) passes them all and reports the strongest
    state any of them reached, so one empty column cannot make a filled card
    read as "not found"."""
    fields = (field, *extra_fields)
    states = [field_status(meta, f, getattr(product, f, None)) for f in fields]
    best = min(states, key=lambda s: _STATUS_RANK.get(s, 99))
    scores = [_confidence(meta, f, s) for f, s in zip(fields, states) if s != 'not_found']
    return {'status': best, 'confidence': max(scores) if scores else 0}


def avatar_letter(name: str | None) -> str:
    text = (name or '').strip()
    return text[0].upper() if text else '?'


def review_status(product) -> str:
    """The hero status pill: verified / needs_verification / found."""
    if product.status == 'verified':
        return 'verified'
    meta = product.field_meta or {}
    reviewed = any((meta.get(f) or {}).get('review') == 'accepted'
                   for f in ('description', 'key_features', 'main_use_case'))
    return 'needs_verification' if reviewed else 'found'


def last_updated(product) -> str:
    """"14 Nov 2026, 04:15 PM".

    Built from `stamp.day` rather than a strftime directive on purpose: the
    no-pad forms are %-d on glibc but %#d on Windows, and the unsupported one
    raises ValueError rather than degrading, which 500'd this endpoint on a
    Windows host. Everything else here is portable."""
    stamp = getattr(product, 'updated_at', None) or getattr(product, 'verified_at', None)
    if not stamp:
        return ''
    return f'{stamp.day} ' + stamp.strftime('%b %Y, %I:%M %p')


def money(amount, currency: str | None, is_custom: bool) -> str:
    if is_custom or amount is None:
        return 'Custom'
    symbol = CURRENCY_SYMBOL.get((currency or 'USD').upper(), '')
    number = Decimal(str(amount))
    whole = number == number.to_integral_value()
    return f'{symbol}{int(number):,}' if whole else f'{symbol}{number:,.2f}'


UNCONFIRMED_PERIOD = 'annual_or_monthly_unconfirmed'


def _period_from(text: str) -> str:
    text = (text or '').lower()
    if re.search(r'/\s*(mo|month)\b|per\s+month|monthly', text):
        return '/month'
    if re.search(r'/\s*(yr|year)\b|per\s+year|annual', text):
        return '/year'
    return ''


def billing_period(plan) -> str:
    """'/month' | '/year' | '' for one plan.

    The source quote wins: extraction stores the literal period as
    'annual_or_monthly_unconfirmed' until a human confirms it, and that
    sentinel contains the substring "monthly", so matching against it labelled
    every unconfirmed plan /month — including "$36,000 / yr"."""
    if plan.is_custom:
        return ''
    from_quote = _period_from(plan.quote)
    if from_quote:
        return from_quote
    period = plan.period if plan.period != UNCONFIRMED_PERIOD else ''
    from_fields = _period_from(f'{plan.unit or ""} {period or ""}')
    if from_fields:
        return from_fields
    if plan.unit:
        return f'/{plan.unit}'
    return ''


AMOUNT = re.compile(r'(?P<cur>\$|USD\s*|€|£|₹)\s*'
                    r'(?P<n>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)')


def _price_from_text(text: str) -> dict | None:
    """'Starts at $185/mo' -> {'price': '$185', 'billingPeriod': '/month'}."""
    match = AMOUNT.search(text or '')
    if not match:
        return None
    symbol = {'€': 'EUR', '£': 'GBP', '₹': 'INR'}.get(match.group('cur').strip(), 'USD')
    return {'price': money(Decimal(match.group('n').replace(',', '')), symbol, False),
            'billingPeriod': _period_from(text), 'currency': symbol}


def derived_plans(product, company=None) -> list[dict]:
    """Plans rebuilt from business_model when pricing_items has no rows.

    Extraction only writes a PricePlan when it finds an explicit plan/amount
    pair on a pricing page, but the business_model JSONB often still holds the
    tier names ('pricing_tiers', 'plan_name') and published amounts ('price',
    'pricing_summary') the crawler read. Without this the card shows no
    pricing at all for a product whose plans we actually know.

    Marked `derived` so the UI can show them as unconfirmed rather than as
    verified plan rows."""
    model = product.business_model if isinstance(product.business_model, dict) else {}
    if not model and company is not None and isinstance(company.business_model, dict):
        model = company.business_model
    names = _list(_pick(model, 'plan_name', 'pricing_tiers', 'plans', 'tiers'))
    prices = _list(_pick(model, 'price', 'prices', 'published_prices', 'pricing_summary'))
    parsed = [p for p in (_price_from_text(text) for text in prices) if p]
    rows = []
    # Pair a tier with its amount only when the two lists line up; otherwise
    # guessing which price belongs to which tier would invent a fact.
    paired = len(parsed) == len(names) and names
    for index, name in enumerate(names):
        amount = parsed[index] if paired else {}
        rows.append({'id': None, 'name': name, 'price': amount.get('price', ''),
                     'billingPeriod': amount.get('billingPeriod', ''),
                     'features': prices[index] if paired else '',
                     'currency': amount.get('currency', ''), 'is_custom': False,
                     'price_amount': None, 'derived': True})
    if not rows:
        for index, amount in enumerate(parsed):
            rows.append({'id': None, 'name': prices[index][:60], 'price': amount['price'],
                         'billingPeriod': amount['billingPeriod'], 'features': prices[index],
                         'currency': amount['currency'], 'is_custom': False,
                         'price_amount': None, 'derived': True})
    return rows[:8]


def plan_view(plan) -> dict:
    return {
        'id': plan.id,
        'name': plan.name or '',
        'price': money(plan.price_amount, plan.currency, plan.is_custom),
        'billingPeriod': billing_period(plan),
        'features': plan.quote or '',
        'currency': plan.currency or '',
        'is_custom': bool(plan.is_custom),
        'price_amount': float(plan.price_amount) if plan.price_amount is not None else None,
        'derived': False,
    }


def business_model_labels(value) -> list[str]:
    """business_model JSONB -> the short badge labels the Overview card shows."""
    if not isinstance(value, dict):
        return _list(value)
    labels = []
    for alias in ('model', 'payment_model', 'pricing_model', 'customer_type', 'revenue_model',
                  'sales_motion', 'delivery', 'summary'):
        picked = _pick(value, alias)
        if picked:
            labels.extend(_list(picked))
    labels.extend(_list(_pick(value, 'revenue_streams', 'pricing_tiers', 'plans')))
    seen, out = set(), []
    for label in labels:
        key = label.lower()
        if key not in seen and len(label) <= 60:
            seen.add(key)
            out.append(label)
    return out[:6]


def pricing_model_label(product, plans, company=None) -> str:
    # 'model'/'revenue_model' before 'payment_model': the latter often holds a
    # billing cycle ('monthly'), which is the billingCycle field, not the model.
    # 'payment_model' usually holds a billing cycle ('monthly'), which belongs
    # in billingCycle — so the company's real model beats the product's cycle.
    strong = ('pricing_model', 'model', 'revenue_model', 'business_model')
    models = [product.business_model] + ([company.business_model] if company is not None else [])
    picked = next((v for v in (_pick(m, *strong) for m in models) if v), None)
    if not picked:
        picked = next((v for v in (_pick(m, 'payment_model') for m in models) if v), None)
    if picked:
        return _text(picked)
    if any(p.is_custom for p in plans) and len(plans) == 1:
        return 'Custom pricing'
    if plans:
        return 'Subscription' if any(billing_period(p) for p in plans) else 'One-time'
    return ''


def billing_cycle_label(plans) -> str:
    cycles = {billing_period(p).lstrip('/') for p in plans if billing_period(p)}
    if not cycles:
        return 'Needs confirmation' if plans else ''
    return ' & '.join(sorted(c.capitalize() for c in cycles))


MAX_TAG = 28


def _tag_text(value: str) -> str:
    """Trim a phrase down to something that fits a pill badge.

    Extracted values are written as prose ("Regulated sectors (healthcare,
    finance, legal, etc.)"), which overflows a badge; the leading clause is
    the part that identifies it."""
    text = re.sub(r'\s+', ' ', (value or '').strip(' .,;'))
    text = re.sub(r'\s*\(.*?\)', '', text).strip()          # drop parentheticals
    head = re.split(r'\s+(?:and|or)\s+|[,;/]| - ', text)[0].strip()
    if 2 <= len(head) <= MAX_TAG:
        return head
    return text[:MAX_TAG].rstrip(' ,;-') if text else ''


def product_tags(product, plans, company=None) -> list[str]:
    """The grid card's pill badges, from real classification data only.

    Ordered most- to least-specific, and deliberately wide: for many products
    category/product_type are empty while the classification actually lives in
    target_market, icp or business_model, which left the card with no badges
    at all."""
    sources = [
        product.category, product.product_type,
        _pick(product.target_market, 'industries', 'target_industries', 'industry', 'verticals'),
        _pick(product.icp, 'industry', 'industry_focus', 'industries'),
        _pick(product.target_market, 'segment', 'target_segments', 'use_cases'),
        _pick(product.business_model, 'model', 'customer_type', 'revenue_model', 'sales_motion'),
    ]
    if company is not None:
        sources.append(company.industry)
        sources.append(_pick(company.business_model, 'model', 'customer_type', 'revenue_model'))
    tags = []
    for value in sources:
        tags.extend(_list(value))
    seen, out = set(), []
    for raw in tags:
        tag = _tag_text(raw)
        key = tag.lower()
        if tag and key not in seen:
            seen.add(key)
            out.append(tag)
        if len(out) == 3:
            break
    return out


def field_meta_view(meta: dict | None, source_urls: dict[int, str]) -> dict:
    """field_meta in the UI's shape: {field: {source, score, review, quote}}."""
    out = {}
    for field, entry in (meta or {}).items():
        if not isinstance(entry, dict):
            continue
        source_id = entry.get('source_id')
        out[field] = {
            'source': source_urls.get(source_id, '') if source_id else '',
            'source_id': source_id,
            'score': _confidence(meta, field),
            'review': entry.get('review') or 'pending',
            'quote': entry.get('quote') or '',
            'status': entry.get('status') or '',
        }
    return out


def overview_view(product, meta) -> dict:
    return {
        **_section(product, meta, 'description', 'category', 'product_type', 'business_model', 'name'),
        'productName': product.name or '',
        'category': product.category or '',
        'productType': _list(product.product_type),
        'businessModel': business_model_labels(product.business_model),
        'website': product.website_url or '',
        'description': product.description or '',
    }


def key_features_view(product, meta) -> dict:
    return {**_section(product, meta, 'key_features'), 'items': _list(product.key_features)}


def value_proposition_view(product, meta) -> dict:
    value, used = product.value_proposition, set()
    insight = _consume(value, used, 'aiInsight', 'summary', 'core', 'statement', 'value',
                       'proposition', 'unique')
    benefits = _consume(value, used, 'keyBenefits', 'benefits', 'key_benefits', 'advantages',
                        'outcomes', 'problems_solved')
    return {
        **_section(product, meta, 'value_proposition'),
        'aiInsight': _text(insight) or _text(product.positioning),
        'keyBenefits': _list(benefits),
        'additional': _additional(value, used),
    }


def target_market_view(product, meta) -> dict:
    value, used = product.target_market, set()
    return {
        **_section(product, meta, 'target_market'),
        'segment': _text(_consume(value, used, 'segment', 'target_segments', 'target',
                                  'market_segment', 'summary')),
        'industries': _list(_consume(value, used, 'industries', 'target_industries', 'industry',
                                     'verticals')),
        'buyers': _text(_consume(value, used, 'buyers', 'buyer_roles', 'decision_makers', 'roles',
                                 'target_users')),
        'region': _text(_consume(value, used, 'region', 'regions', 'geography', 'geo', 'countries',
                                 'markets')),
        'painPoints': _list(_consume(value, used, 'painPoints', 'pain_points', 'needs', 'problems',
                                     'problems_solved')),
        'reachable': _text(_consume(value, used, 'reachable', 'channels', 'where_to_reach', 'use_cases')),
        'additional': _additional(value, used),
    }


def icp_view(product, meta) -> dict:
    value, used = product.icp, set()
    return {
        **_section(product, meta, 'icp'),
        'targetAudience': _text(_consume(value, used, 'targetAudience', 'target_audience', 'audience',
                                         'who', 'target', 'icp_hypotheses')),
        'industry': _text(_consume(value, used, 'industry', 'industry_focus', 'industries', 'verticals')),
        'companySize': _text(_consume(value, used, 'companySize', 'company_size', 'size',
                                      'company_sizes', 'employee_count')),
        'geography': _text(_consume(value, used, 'geography', 'geo', 'region', 'regions', 'location',
                                    'countries')),
        'decisionMakers': _text(_consume(value, used, 'decisionMakers', 'decision_makers', 'buyers',
                                         'buyer_roles', 'roles', 'titles')),
        'customerPainPoints': _text(_consume(value, used, 'customerPainPoints', 'customer_pain_points',
                                             'pain_points', 'needs', 'problems', 'challenges')),
        'additional': _additional(value, used),
    }


def positioning_view(product, meta) -> dict:
    return {**_section(product, meta, 'positioning'), 'statement': _text(product.positioning)}


def pricing_view(product, meta, plans, company=None) -> dict:
    rows = [plan_view(p) for p in plans]
    derived = not rows
    if derived:
        rows = derived_plans(product, company)
    status = field_status(meta, 'pricing', plans)
    if derived and rows:
        # Rebuilt from business_model rather than read off a pricing page, so
        # it needs confirming even though we do have the plan names.
        status = 'needs_confirmation'
    return {
        'status': status,
        'confidence': (_confidence(meta, 'pricing') or _confidence(meta, 'pricing_status')
                       or STATUS_CONFIDENCE.get(status, 0)),
        'pricingModel': pricing_model_label(product, plans, company),
        'billingCycle': (billing_cycle_label(plans) if plans
                         else ' & '.join(sorted({r['billingPeriod'].lstrip('/').capitalize()
                                                 for r in rows if r['billingPeriod']}))
                         or ('Needs confirmation' if rows else '')),
        'currency': next((p.currency for p in plans if p.currency),
                         next((r['currency'] for r in rows if r.get('currency')), 'USD')),
        'plans': rows,
        'derived': derived and bool(rows),
    }


def product_card(product, company, plans) -> dict:
    """The product grid card."""
    first = next((p for p in plans if not p.is_custom), None) or (plans[0] if plans else None)
    if first is not None:
        price, period = money(first.price_amount, first.currency, first.is_custom), billing_period(first)
    else:
        # No pricing_items rows: fall back to what business_model recorded,
        # rather than showing a product with no price at all.
        priced = next((r for r in derived_plans(product, company) if r['price']), None)
        price = priced['price'] if priced else ''
        period = priced['billingPeriod'] if priced else ''
    return {
        'id': product.id,
        'name': product.name or '',
        'avatarLetter': avatar_letter(product.name),
        'domain': (company.domain if company else '') or '',
        'status': review_status(product),
        'description': product.description or '',
        'tags': product_tags(product, plans, company),
        'price': price,
        'billingPeriod': period,
        'model': pricing_model_label(product, plans, company),
    }


def product_view(product, company, plans, meta_view: dict) -> dict:
    """The verification detail page's sections, under the exact names it binds
    to. Each section that replaces a raw column also leaves the raw value under
    a `*_raw` key, so a PATCH can round-trip the original shape (key_features is
    still a plain list on the way back in) and nothing the extractor produced is
    dropped on the way out."""
    meta = product.field_meta
    return {
        'avatarLetter': avatar_letter(product.name),
        'domain': (company.domain if company else '') or '',
        'review_status': review_status(product),
        'last_updated': last_updated(product),
        'overview': overview_view(product, meta),
        'key_features': key_features_view(product, meta),
        'key_features_raw': _list(product.key_features),
        'value_proposition': value_proposition_view(product, meta),
        'value_proposition_raw': product.value_proposition or {},
        'target_market': target_market_view(product, meta),
        'target_market_raw': product.target_market or {},
        'icp': icp_view(product, meta),
        'icp_raw': product.icp or {},
        'positioning': positioning_view(product, meta),
        'positioning_raw': product.positioning or '',
        'pricing': pricing_view(product, meta, plans, company),
        'field_meta': meta_view,
        'field_meta_raw': meta or {},
    }
