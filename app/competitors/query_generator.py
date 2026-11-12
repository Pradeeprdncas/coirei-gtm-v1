import json
import re

from app.services.llm import json_completion


def compact_product(snapshot: dict) -> dict:
    company = snapshot.get("company", {})
    product = snapshot.get("product", {})
    intelligence = snapshot.get("intelligence", {})

    base = {
        "product": product.get("name"),
        "category": product.get("category"),
        "description": product.get("description"),
        "features": (
            product.get("key_features") or []
        )[:6],
        "use_case": product.get("main_use_case"),
        "business_model": product.get("business_model"),
        "positioning": product.get("positioning"),
        "value_proposition": product.get(
            "value_proposition"
        ),
        "target_market": product.get("target_market"),
        "icp": product.get("icp"),
        "company_country": company.get("country"),
        "company_market": company.get("market_segment"),
    }

    if intelligence:
        if intelligence.get("industry") and not base.get("category"):
            base["category"] = intelligence["industry"]
        if intelligence.get("key_messages"):
            base["key_messages"] = intelligence["key_messages"][:4]
        if intelligence.get("selling_points"):
            base["selling_points"] = intelligence["selling_points"][:4]
        if intelligence.get("target_segments"):
            base["target_segments"] = intelligence["target_segments"]
        if intelligence.get("differentiators"):
            base["differentiators"] = intelligence["differentiators"][:3]
        if intelligence.get("named_customers"):
            base["known_customers"] = intelligence["named_customers"][:5]

    return base


def _intelligence_queries(intelligence: dict, name: str) -> list[str]:
    """Generate extra queries from intelligence data that the snapshot may lack."""
    queries = []
    selling_points = intelligence.get("selling_points")
    if isinstance(selling_points, list) and selling_points:
        queries.append(f"software that {selling_points[0][:80]}")
    target = intelligence.get("target_segments")
    if isinstance(target, str) and target:
        queries.append(f"{target[:60]} software tools competitors")
    elif isinstance(target, list) and target:
        queries.append(f"{target[0][:60]} software tools competitors")
    differentiators = intelligence.get("differentiators")
    if isinstance(differentiators, list) and differentiators:
        queries.append(f"alternatives for {differentiators[0][:80]}")
    customers = intelligence.get("named_customers")
    if isinstance(customers, list) and len(customers) >= 2:
        queries.append(f"tools used by {customers[0][:40]} and {customers[1][:40]}")
    return queries


async def generate_queries(
    snapshot: dict,
    count: int = 5,
) -> list[str]:
    context = compact_product(snapshot)
    intelligence = snapshot.get("intelligence", {})

    name = str(context.get("product") or "software")

    raw_category = str(
        context.get("category")
        or context.get("use_case")
        or "software"
    )
    # Truncate at word boundary so queries don't end mid-word.
    if len(raw_category) > 60:
        raw_category = raw_category[:60].rsplit(" ", 1)[0]
    category = raw_category

    region = str(
        context.get("company_country") or ""
    )[:70]

    fallback = [
        f"{name} alternatives competitors",
        f"best {category} products",
        f"{category} software competitors",
        f"{category} platforms for businesses {region}",
        f"alternatives to {name} for same customers",
    ]

    if intelligence:
        fallback = _intelligence_queries(intelligence, name) + fallback

    ai = await json_completion(
        (
            "You generate SEARCH ENGINE QUERIES to find "
            "directly competing software products. "
            "Return JSON with exactly five distinct queries. "
            "Focus on products, use cases, target audiences, "
            "business models and service geography. "
            "Do not invent specific competitor names. "
            "A competitor may be based in another country "
            "but sell to the same target geography. "
            'Output {"queries":["..."]}.'
        ),
        json.dumps(
            context,
            ensure_ascii=False,
            default=str,
        )[:3500],
    )

    raw = ai.get("queries", [])
    if not isinstance(raw, list):
        raw = []

    queries = []
    seen = set()

    for value in [*raw, *fallback]:
        if not isinstance(value, str):
            continue

        query = re.sub(r"\s+", " ", value).strip()
        query = query[:160]

        if len(query) < 8:
            continue

        if query.casefold() in seen:
            continue

        seen.add(query.casefold())
        queries.append(query)

        if len(queries) >= count:
            break

    return queries