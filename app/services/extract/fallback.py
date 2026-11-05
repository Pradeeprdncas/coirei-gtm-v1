"""Evidence-only, low-cost fallbacks used when Ollama is down or rejects JSON.

This module does not invent facts: every returned item includes an exact quote from
one of the supplied source texts. Its output is intentionally pending human review.
"""
import re

NOISE = re.compile(r'(?i)^(get started|download|learn more|sign in|sign up|pricing|contact us|all apps|home|about|menu|skip to|privacy|terms|cookies|tab \d+)\s*[.!?]?$')
FEATURE_TERMS = re.compile(r'(?i)\b(dictat\w*|voice|speech|text|transcrib\w*|customiz\w*|personaliz\w*|collaborat\w*|integrat\w*|automate|workflow|support|summari\w*|notetak\w*|translate|edit|refine|share|knowledge|agent|analytics|security|API|context|accuracy)\b')
VALUE_TERMS = re.compile(r'(?i)\b(help\w*|faster|easier|improv\w*|save|reduce|productiv\w*|efficien\w*|turn\w*|enable\w*|automate\w*|simplif\w*)\b')
AUDIENCE = re.compile(r'(?i)\b(for (?:every\w+|teams?|individuals?|professionals?|businesses|developers|students|creators|agencies)|built for|designed for|used by|teams can|your team|everyone)\b')


def sentences(text: str) -> list[str]:
    """Return unmodified source substrings, not newly composed claims."""
    found = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        for part in re.split(r'(?<=[.!?])\s+(?=[A-Z])', line):
            quote = part.strip()
            if (35 <= len(quote) <= 300 and not NOISE.fullmatch(quote)
                    and not quote.lower().startswith(('http:', 'https:'))):
                found.append(quote)
    return found


def candidate_sentences(sources, pattern, *, types=('features', 'product', 'home', 'about'), limit=6):
    results, seen = [], set()
    ranked = sorted(sources, key=lambda s: (types.index(s.page_type)
                    if s.page_type in types else len(types), s.url))
    for source in ranked:
        if source.page_type not in types:
            continue
        for quote in sentences(source.text):
            if not pattern.search(quote):
                continue
            normalized = re.sub(r'\W+', '', quote.lower())[:90]
            if normalized in seen:
                continue
            seen.add(normalized)
            results.append({'value': quote[:260], 'quote': quote, 'source': source})
            if len(results) >= limit:
                return results
    return results


def feature_fallback(sources, *, limit=6):
    """Avoid generic feature words when no explanatory line was present."""
    return candidate_sentences(sources, FEATURE_TERMS, limit=limit)


def use_case_fallback(sources):
    for item in candidate_sentences(sources, FEATURE_TERMS, types=('home', 'product', 'features', 'about'), limit=15):
        q = item['quote'].lower()
        if any(x in q for x in ('use ', 'use it ', 'you can', 'teams use', 'dictat', 'voice to text', 'voice-to-text', 'build ', 'create ', 'automate', 'convert')):
            return item
    return None


def positioning_fallback(sources):
    for item in candidate_sentences(sources, VALUE_TERMS, types=('home', 'product', 'features', 'about'), limit=15):
        if any(t in item['quote'].lower() for t in ('help', 'turn', 'enable', 'voice', 'platform', 'productiv', 'faster')):
            return item
    return None


def pricing_model_fallback(sources):
    """Extract only unmistakable first-party billing signals from pricing pages."""
    for source in sources:
        if source.page_type != 'pricing':
            continue
        content = source.text or ''
        modes = []
        if re.search(r'(?i)\b(monthly|annually|annual billing|billed annually|per month|/mo)\b', content):
            modes.append('subscription')
        if re.search(r'(?i)\b(usage.based|pay.as.you.go|per outcome|credits per|consumption.based)\b', content):
            modes.append('usage_based')
        if re.search(r'(?i)\b(contact sales|request pricing|custom pricing|talk to sales)\b', content):
            modes.append('sales_led')
        if not modes:
            continue
        evidence = next((line.strip() for line in content.splitlines()
                         if 2 <= len(line.strip()) <= 240 and
                         re.search(r'(?i)\b(monthly|annual|/mo|usage.based|per outcome|contact sales|custom pricing)\b', line)), None)
        if not evidence:
            continue
        return {'value': {'summary': ', '.join(modes).replace('_', ' '), 'models': modes},
                'quote': evidence, 'source': source}
    return None


def market_segment_fallback(sources):
    items = candidate_sentences(sources, AUDIENCE, types=('home', 'about', 'product', 'features'), limit=8)
    if not items:
        return None
    chosen = items[0]
    return {'value': {'summary': chosen['value'], 'source_type': 'first_party_audience'},
            'quote': chosen['quote'], 'source': chosen['source']}
