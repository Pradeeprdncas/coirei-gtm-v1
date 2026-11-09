import json
import re

from app.services.llm import json_completion


def compact_product(snapshot: dict) -> dict:
    company = snapshot.get("company", {})
    product = snapshot.get("product", {})

    return {
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


async def generate_queries(
    snapshot: dict,
    count: int = 5,
) -> list[str]:
    context = compact_product(snapshot)

    name = str(context.get("product") or "software")
    category = str(
        context.get("category")
        or context.get("use_case")
        or "software"
    )[:100]

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