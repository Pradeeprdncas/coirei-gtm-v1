import json
import re

from app.services.llm import json_completion
from app.competitors.query_generator import (
    compact_product,
)


FIELDS = [
    "company_name",
    "product_name",
    "description",
    "category",
    "product_type",
    "key_features",
    "main_use_case",
    "business_model",
    "pricing",
    "market_segment",
    "icp",
    "geography_served",
    "headquarters",
    "company_size",
    "value_proposition",
    "positioning",
    "market_size",
]

# Weights add up to 100.
WEIGHTS = {
    "product_overlap": 30,
    "use_case": 20,
    "customer_overlap": 15,
    "market_segment": 10,
    "geography": 15,
    "business_model": 5,
    "positioning": 5,
}


def clean(text):
    return re.sub(
        r"\s+", " ", str(text or "")
    ).strip().casefold()


def valid_evidence(
    item: dict,
    sources: list[dict],
) -> dict | None:
    if not isinstance(item, dict):
        return None

    source_id = item.get("source_id")
    quote = item.get("quote")

    if not str(source_id).isdigit():
        return None

    if not isinstance(quote, str) or len(
        quote.strip()
    ) < 8:
        return None

    source = next(
        (
            s for s in sources
            if s["source_id"] == int(source_id)
        ),
        None,
    )

    if not source:
        return None

    if clean(quote) not in clean(source["text"]):
        return None

    return {
        "source_id": int(source_id),
        "source_url": source["url"],
        "quote": quote[:450],
    }


async def extract_competitor_profile(
    sources: list[dict],
) -> dict:
    # Each call gets only bounded first-party evidence.
    evidence = "\n\n".join(
        (
            f'SOURCE_ID={s["source_id"]}\n'
            f'URL={s["url"]}\n'
            f'PAGE_TYPE={s["page_type"]}\n'
            f'TEXT={s["text"][:1200]}'
        )
        for s in sources[:4]
    )

    ai = await json_completion(
        (
            "Extract a structured software-company and "
            "product profile from its OWN website. "
            "Return JSON only, with a 'fields' object. "
            "Every field is either null, one object "
            '{"value":"...","quote":"exact text",'
            '"source_id":123}, or, for key_features, '
            "an array of such objects. "
            "Do not confuse pricing tiers with products. "
            "Do not infer geography served from headquarters. "
            "Do not invent market size, customer counts, "
            "prices or employee counts. "
            "Only extract facts whose exact quotes appear "
            "in the supplied source text. "
            "Include company_name, product_name, description, "
            "category, product_type, key_features, "
            "main_use_case, business_model, pricing, "
            "market_segment, icp, geography_served, "
            "headquarters, company_size, value_proposition, "
            "positioning and market_size."
        ),
        (
            evidence
            + "\nReturn JSON: "
            + '{"fields":{"company_name":null,'
            + '"description":null,"key_features":[]}}'
        ),
    )

    raw_fields = ai.get("fields", {})
    if not isinstance(raw_fields, dict):
        raw_fields = {}

    profile = {}

    for field in FIELDS:
        value = raw_fields.get(field)

        if field == "key_features":
            verified_features = []

            if isinstance(value, list):
                for entry in value[:10]:
                    provenance = valid_evidence(
                        entry, sources
                    )

                    if provenance and isinstance(
                        entry.get("value"), str
                    ):
                        verified_features.append({
                            "value": entry["value"][:300],
                            **provenance,
                        })

            profile[field] = verified_features
            continue

        provenance = valid_evidence(
            value, sources
        )

        if (
            provenance
            and isinstance(value.get("value"), str)
            and value["value"].strip()
        ):
            profile[field] = {
                "value": value["value"][:2000],
                **provenance,
            }
        else:
            profile[field] = None

    return profile


async def score_competitor(
    own_snapshot: dict,
    competitor_profile: dict,
    sources: list[dict],
) -> dict:
    own = compact_product(own_snapshot)

    ai = await json_completion(
        (
            "You are a B2B competitive-intelligence analyst. "
            "Decide whether the candidate product competes "
            "with OUR PRODUCT, not merely our parent company. "
            "Analyze actual feature overlap, core use case, "
            "ideal buyers, market segment, geography served, "
            "business model and positioning. "
            "A US-headquartered vendor can compete in India "
            "if it serves Indian customers. Headquarters "
            "alone is NOT evidence of service geography. "
            "If a dimension cannot be established, return "
            "score=null; never guess. "
            "Each non-null score MUST have a source_id "
            "and an exact quote from the candidate evidence. "
            "Use integer scores 0 to 5: "
            "0 = clearly not aligned, "
            "5 = almost identical competition. "
            "Return a JSON object with decision "
            "(competitor, possible or not_competitor), "
            "dimensions, and explanation. "
            "Dimensions: product_overlap, use_case, "
            "customer_overlap, market_segment, geography, "
            "business_model, positioning. "
            "Each dimension must contain score, source_id, "
            "quote and reason."
        ),
        (
            "OUR PRODUCT:\n"
            + json.dumps(
                own, ensure_ascii=False,
                default=str,
            )[:2400]
            + "\n\nCANDIDATE VERIFIED FIELDS:\n"
            + json.dumps(
                competitor_profile,
                ensure_ascii=False,
            )[:6000]
            + "\n\nReturn JSON only."
        ),
    )

    raw = ai.get("dimensions", {})
    if not isinstance(raw, dict):
        raw = {}

    details = {}
    weighted_score = 0.0
    covered_weight = 0

    for dimension, weight in WEIGHTS.items():
        item = raw.get(dimension)

        if not isinstance(item, dict):
            details[dimension] = {
                "score": None,
                "weight": weight,
                "reason": "Insufficient evidence",
            }
            continue

        provenance = valid_evidence(
            item, sources
        )

        value = item.get("score")

        # Explicitly reject guessed, ungrounded scores.
        if (
            not provenance
            or type(value) is not int
            or value < 0
            or value > 5
        ):
            details[dimension] = {
                "score": None,
                "weight": weight,
                "reason": "Insufficient verified evidence",
            }
            continue

        weighted_score += (
            weight * value / 5
        )
        covered_weight += weight

        details[dimension] = {
            "score": value,
            "weight": weight,
            "reason": str(
                item.get("reason") or ""
            )[:350],
            **provenance,
        }

    percent = round(weighted_score)
    coverage = round(
        covered_weight / 100, 3
    )

    product_score = details[
        "product_overlap"
    ]["score"]

    use_case_score = details[
        "use_case"
    ]["score"]

    decision = ai.get("decision")

    if (
        decision == "not_competitor"
        and product_score is not None
        and product_score <= 2
    ):
        status = "rejected"

    elif (
        decision == "competitor"
        and product_score is not None
        and product_score >= 3
        and use_case_score is not None
        and use_case_score >= 3
        and percent >= 65
        and coverage >= 0.60
    ):
        status = "confirmed"

    else:
        status = "possible"

    return {
        "status": status,
        "match_percent": percent,
        "evidence_coverage": coverage,
        "score_details": details,
        "explanation": str(
            ai.get("explanation")
            or "More evidence is needed."
        )[:1000],
        "model_decision": decision,
    }