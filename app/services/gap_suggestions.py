import re

RULES = {
    "business_model": {
        "pages": ("pricing", "home", "about", "product", "document"),
        "keywords": r"subscription|monthly|annually|annual|billing|pricing|free plan|enterprise|per user|per seat|credits|usage.based|contact sales",
        "question": "How does your company charge customers?",
    },
    "icp": {
        "pages": ("home", "about", "product", "features", "pricing", "document"),
        "keywords": r"for teams|for professionals|for individuals|for businesses|for developers|customers|built for|designed for|organizations|target audience",
        "question": "Who is your ideal customer?",
    },
    "key_features": {
        "pages": ("features", "product", "home", "document"),
        "keywords": r"feature|voice|speech|dictat|transcrib|workflow|integrat|collaborat|automat|AI|knowledge|summari|security|sync|language",
        "question": "What are the key features of {product_name}?",
    },
    "main_use_case": {
        "pages": ("product", "features", "home", "document"),
        "keywords": r"helps|use|build|create|write|dictat|convert|automat|transcrib|record|summari|support|deploy",
        "question": "What is the main use case of {product_name}?",
    },
    "description": {
        "pages": ("product", "home", "about", "document"),
        "keywords": r"platform|software|AI|product|tool|helps|service|solution",
        "question": "How would you describe {product_name}?",
    },
    "products": {
        "pages": ("home", "about", "product", "document"),
        "keywords": r"our products|our offerings|products include|platform|portfolio|solutions",
        "question": "Which products does your company sell?",
    },
}

BANNED = re.compile(
    r"\b(install|powershell|pip install|npm install|sudo|"
    r"git clone|cookie consent|privacy policy)\b",
    re.IGNORECASE,
)


def field_kind(field_key: str) -> str:
    return field_key.rsplit(".", 1)[-1]


def page_types_for(field_key: str) -> tuple:
    rule = RULES.get(field_kind(field_key), RULES["description"])
    return rule["pages"]


def question_template(
    field_key: str,
    product_name: str = "this product",
) -> str:
    rule = RULES.get(field_kind(field_key), RULES["description"])
    return rule["question"].format(product_name=product_name)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def excerpts(text: str):
    lines = (text or "").splitlines(keepends=True)

    for i, line in enumerate(lines):
        candidates = [line.strip()]

        # Pricing and feature lists may be split across lines.
        if len(line.strip()) < 30:
            candidates.append("".join(lines[i:i + 2]).strip())
            candidates.append("".join(lines[i:i + 3]).strip())

        for candidate in candidates:
            if 30 <= len(candidate) <= 340:
                yield candidate

            # Extract sentences from long paragraphs.
            for sentence in re.split(r"(?<=[.!?])\s+", candidate):
                if 30 <= len(sentence) <= 340:
                    yield sentence


def fallback_suggestions(
    field_key: str,
    passages: list[dict],
    limit: int = 3,
) -> list[dict]:
    """
    Return actual first-party source statements.

    No fabricated answers.
    No unrelated CLI questions.
    No source mixing between companies.
    """
    rule = RULES.get(field_kind(field_key), RULES["description"])
    pattern = re.compile(rule["keywords"], re.IGNORECASE)

    suggestions = []
    seen = set()

    priority = rule["pages"]

    passages = sorted(
        passages,
        key=lambda p: (
            priority.index(p["page_type"])
            if p.get("page_type") in priority
            else 999
        ),
    )

    for passage in passages:
        if passage.get("page_type") not in priority:
            continue

        if not passage.get("text"):
            continue

        for quote in excerpts(passage["text"]):
            clean = normalize(quote)
            key = clean.lower()

            if key in seen:
                continue

            if BANNED.search(clean):
                continue

            if not pattern.search(clean):
                continue

            seen.add(key)

            suggestions.append({
                "text": clean[:300],
                "quote": quote,
                "source_id": passage["source_id"],
                "source_url": passage.get("url", ""),
                "source": "evidence",
            })

            if len(suggestions) >= limit:
                return suggestions

    return suggestions


def merge_suggestions(
    field_key: str,
    passages: list[dict],
    ai: dict | None = None,
) -> list[dict]:
    """
    Prefer valid AI suggestions.
    Fall back to extracted source statements.
    """
    suggestions = []
    seen = set()

    allowed = {
        int(p["source_id"]): p
        for p in passages
        if p.get("source_id") is not None
    }

    if isinstance(ai, dict):
        for item in ai.get("suggestions", [])[:6]:
            if not isinstance(item, dict):
                continue

            source_id = item.get("source_id")
            quote = item.get("quote")
            answer = item.get("text")

            if not str(source_id).isdigit():
                continue

            source = allowed.get(int(source_id))

            if not source or not quote or not answer:
                continue

            if source.get("page_type") not in page_types_for(field_key):
                continue

            if normalize(quote).lower() not in normalize(
                source.get("text", "")
            ).lower():
                continue

            if BANNED.search(answer):
                continue

            if BANNED.search(quote):
                continue

            # The real anti-hallucination guard is above: the quote must appear
            # verbatim in the cited source. A further "shares a word with its
            # quote" check used to live here too, but an AI answer that properly
            # paraphrases its quote often shares none of its exact words —
            # that silently rejected good AI suggestions and fell through to
            # the cruder keyword-regex fallback below for no real safety gain.

            key = normalize(answer).lower()

            if key in seen:
                continue

            seen.add(key)

            suggestions.append({
                "text": answer[:300],
                "quote": quote,
                "source_id": int(source_id),
                "source_url": source.get("url", ""),
                "source": "evidence",
                "verified_by": "ai",
            })

            if len(suggestions) >= 3:
                return suggestions

    for item in fallback_suggestions(field_key, passages, limit=8):
        key = normalize(item["text"]).lower()

        if key in seen:
            continue

        seen.add(key)
        suggestions.append({**item, "verified_by": "keyword_match"})

        if len(suggestions) >= 3:
            break

    return suggestions[:3]

# Answer shapes the gap-question UI can render. Mirrors the GTM crawler's own
# question vocabulary (gtm_crawler/questions.py TYPES) so one renderer handles
# both question sources.
MULTI_VALUE_FIELDS = {'key_features', 'products'}


def question_type(field_key: str, suggestions: list[dict] | None) -> str:
    """'multi_choice' | 'choice' | 'text' for one gap question.

    A field that holds a list (key_features, the product catalogue) takes
    several answers; anything else with evidence-backed suggestions is a
    single pick; with no suggestions there is nothing to pick from, so it is
    free text."""
    kind = field_kind(field_key)
    if kind in MULTI_VALUE_FIELDS:
        return 'multi_choice'
    return 'choice' if suggestions else 'text'


def question_options(suggestions: list[dict] | None) -> list[dict]:
    """The selectable options for a choice/multi_choice question, each carrying
    its own evidence so the UI can show why it was offered."""
    options = []
    for index, item in enumerate(suggestions or []):
        text = (item or {}).get('text')
        if not text:
            continue
        options.append({
            'id': f'opt-{index + 1}',
            'label': text,
            'value': text,
            'source': item.get('source') or '',
            'source_url': item.get('source_url') or '',
            'quote': item.get('quote') or '',
            'verified_by': item.get('verified_by') or '',
        })
    return options
