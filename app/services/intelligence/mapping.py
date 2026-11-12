"""Pure, conservative mapping from the ZIP's 13-section business-profile API.

No DB access here. Inferred values are review candidates, not verified facts.
Unknown, unsupported and non-public fields are never imported.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED = {"SUPPORTED", "CORROBORATED", "VERIFIED"}
INFERRED = {"MODEL_INFERRED"}

EVIDENCE_QUALITY = {"VERIFIED": 4, "CORROBORATED": 3, "SUPPORTED": 2, "MODEL_INFERRED": 1, "UNKNOWN": 0}


@dataclass(frozen=True)
class Candidate:
    field: str
    value: Any
    evidence_state: str
    confidence: float | None
    source_urls: tuple[str, ...]
    quote: str | None
    origin: str | None

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "value": self.value,
            "evidence_state": self.evidence_state,
            "confidence": self.confidence,
            "source_urls": list(self.source_urls),
            "quote": self.quote,
            "origin": self.origin,
            "requires_review": True,
        }

    @property
    def quality_rank(self) -> int:
        return EVIDENCE_QUALITY.get(self.evidence_state, 0)


def _usable(raw: Any, *, include_inferred: bool) -> bool:
    if not isinstance(raw, dict) or raw.get("access", "public") != "public":
        return False
    if raw.get("value") in (None, "", [], {}):
        return False
    state = str(raw.get("evidence_state") or "").upper()
    return state in SUPPORTED or (include_inferred and state in INFERRED)


def _source(raw: dict) -> tuple[tuple[str, ...], str | None]:
    urls = []
    quote = None
    for source in raw.get("sources") or []:
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in urls:
            urls.append(url[:2048])
        snippet = source.get("quote")
        if quote is None and isinstance(snippet, str) and snippet.strip():
            quote = snippet[:2000]
    return tuple(urls[:20]), quote


def _field_index(report: dict) -> dict[str, dict]:
    fields: dict[str, dict] = {}
    for section in report.get("sections") or []:
        if not isinstance(section, dict) or section.get("key") in {"missing_information", "source_records"}:
            continue
        for raw in section.get("fields") or []:
            if isinstance(raw, dict) and isinstance(raw.get("key"), str):
                fields.setdefault(raw["key"], raw)
    return fields


def _confidence(raw: dict) -> float | None:
    val = raw.get("confidence")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return max(0.0, min(1.0, float(val)))
    return None


def _candidate(name: str, raw: dict, value: Any = None) -> Candidate:
    urls, quote = _source(raw)
    return Candidate(
        field=name,
        value=raw["value"] if value is None else value,
        evidence_state=str(raw.get("evidence_state") or "").upper(),
        confidence=_confidence(raw),
        source_urls=urls,
        quote=quote,
        origin=raw.get("origin"),
    )


def _first_string(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _string_list(value: Any, *, max_items: int = 30) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x.strip() for x in value if isinstance(x, str) and x.strip()][:max_items]


def _as_int(value: Any, min_value: int, max_value: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if min_value <= value <= max_value else None
    if isinstance(value, str) and value.isdecimal():
        n = int(value)
        return n if min_value <= n <= max_value else None
    return None


def _group_candidate(
    name: str, parts: list[tuple[str, dict]], *, summary_key: str | None = None,
    max_list_len: int = 8,
) -> Candidate | None:
    if not parts:
        return None
    value = {}
    urls = []
    quote = None
    states = []
    confidences = []
    for key, raw in parts:
        val = raw["value"]
        if isinstance(val, list) and len(val) > max_list_len:
            val = val[:max_list_len]
        value[key] = val
        states.append(str(raw.get("evidence_state") or "").upper())
        if _confidence(raw) is not None:
            confidences.append(_confidence(raw))
        src_urls, src_quote = _source(raw)
        for url in src_urls:
            if url not in urls:
                urls.append(url)
        quote = quote or src_quote
    if summary_key and summary_key in value:
        value["summary"] = value[summary_key]
    state = "MODEL_INFERRED" if any(s in INFERRED for s in states) else "SUPPORTED"
    return Candidate(name, value, state, min(confidences) if confidences else None,
                     tuple(urls[:20]), quote, "model" if state == "MODEL_INFERRED" else "crawler")


def company_candidates(report: dict, *, include_inferred: bool = False) -> dict[str, Candidate]:
    """Candidate company values, restricted to V1 editable fields and types."""
    idx = _field_index(report)
    output: dict[str, Candidate] = {}

    # --- Scalar string fields ---
    scalar_fields = {
        "company_description": "description",
        "market_domain": "industry",
        "headquarters": "headquarters",
        "company_country": "country",
        "company_size": "size_band",
    }
    for key, v1_key in scalar_fields.items():
        raw = idx.get(key)
        if not _usable(raw, include_inferred=include_inferred):
            continue
        value = _first_string(raw["value"])
        if value is None:
            continue
        output[v1_key] = _candidate(v1_key, raw, value)

    # --- Integer fields ---
    int_fields = {
        "employee_count": ("employee_count", 1, 10_000_000),
        "founded_year": ("founded_year", 1800, 2100),
    }
    for key, (v1_key, lo, hi) in int_fields.items():
        raw = idx.get(key)
        if not _usable(raw, include_inferred=include_inferred):
            continue
        value = _as_int(raw["value"], lo, hi)
        if value is None:
            continue
        output[v1_key] = _candidate(v1_key, raw, value)

    # --- Socials: collect social profiles, contact info into socials JSONB ---
    socials_parts: list[tuple[str, dict]] = []
    for key in ("social_profiles", "contact_email", "contact_phone", "contact_page"):
        raw = idx.get(key)
        if raw and _usable(raw, include_inferred=include_inferred):
            socials_parts.append((key, raw))
    if socials_parts:
        socials_value = {}
        urls_all = []
        quote_all = None
        states_all = []
        confs_all = []
        for key, raw in socials_parts:
            val = raw["value"]
            if key == "social_profiles":
                if isinstance(val, list):
                    for entry in val:
                        if isinstance(entry, dict) and entry.get("platform") and entry.get("url"):
                            socials_value[entry["platform"]] = entry["url"]
                        elif isinstance(entry, str) and entry.startswith("http"):
                            for site in ("linkedin.com", "youtube.com", "instagram.com",
                                         "x.com", "twitter.com", "github.com", "facebook.com"):
                                if site in entry:
                                    socials_value[site] = entry
                                    break
            elif key == "contact_email":
                emails = _string_list(val) if isinstance(val, list) else ([val] if isinstance(val, str) else [])
                if emails:
                    socials_value["contact_emails"] = emails
            elif key == "contact_phone":
                phones = _string_list(val) if isinstance(val, list) else ([val] if isinstance(val, str) else [])
                if phones:
                    socials_value["contact_phones"] = phones
            elif key == "contact_page":
                pages = _string_list(val) if isinstance(val, list) else ([val] if isinstance(val, str) else [])
                if pages:
                    socials_value["contact_pages"] = pages
            states_all.append(str(raw.get("evidence_state") or "").upper())
            c = _confidence(raw)
            if c is not None:
                confs_all.append(c)
            su, sq = _source(raw)
            for u in su:
                if u not in urls_all:
                    urls_all.append(u)
            quote_all = quote_all or sq

        if socials_value:
            state = "MODEL_INFERRED" if any(s in INFERRED for s in states_all) else "SUPPORTED"
            output["socials"] = Candidate(
                "socials", socials_value, state,
                min(confs_all) if confs_all else None,
                tuple(urls_all[:20]), quote_all,
                "model" if state == "MODEL_INFERRED" else "crawler",
            )

    # --- JSONB group fields ---
    groups = {
        "business_model": ("business_model", "customer_type", "revenue_model", "delivery", "sales_motion",
                           "pricing_summary", "plan_name", "price"),
        "market_segment": ("target_segments", "business_types", "company_sizes", "buyer_roles",
                           "target_industries", "main_use_case", "customer_concentration"),
        "value_proposition": ("positioning_statement", "product_positioning", "selling_points",
                              "differentiators", "tagline", "key_messages", "tone_of_voice"),
        "icp": ("icp_hypotheses", "buyer_roles", "target_industries", "company_sizes"),
    }
    for v1_key, source_keys in groups.items():
        parts = [(k, idx[k]) for k in source_keys if k in idx and _usable(idx[k], include_inferred=include_inferred)]
        if not parts:
            continue
        candidate = _group_candidate(
            v1_key, parts, summary_key="positioning_statement" if v1_key == "value_proposition" else None,
        )
        if candidate:
            output[v1_key] = candidate

    return output


def product_candidates(report: dict, *, include_inferred: bool = False) -> list[dict]:
    """Top-level named offerings with all available detail fields."""
    results: list[dict] = []
    seen = set()
    for section in report.get("sections") or []:
        if not isinstance(section, dict) or section.get("key") != "products_services":
            continue
        for raw in section.get("items") or []:
            if not _usable(raw, include_inferred=include_inferred):
                continue
            if raw.get("key") != "product":
                continue
            name = _first_string(raw.get("value"))
            if not name or len(name) > 255 or name.casefold() in seen or name.isdecimal():
                continue
            seen.add(name.casefold())
            details = raw.get("details") or {}
            if not isinstance(details, dict):
                details = {}
            desc = _first_string(details.get("description"))
            category = _first_string(details.get("category"))
            features = details.get("key_features") or []
            if not isinstance(features, list):
                features = []
            features = [x.strip() for x in features if isinstance(x, str) and x.strip()][:30]
            main_use_case = _first_string(details.get("use_cases"))
            pricing_model = _first_string(details.get("pricing_model"))
            target_users = _first_string(details.get("target_users"))
            benefits = details.get("benefits")
            if isinstance(benefits, list):
                benefits = [x.strip() for x in benefits if isinstance(x, str) and x.strip()][:20]
            else:
                benefits = []
            problems_solved = details.get("problems_solved")
            if isinstance(problems_solved, list):
                problems_solved = [x.strip() for x in problems_solved if isinstance(x, str) and x.strip()][:20]
            else:
                problems_solved = []

            prices = details.get("prices") or []
            if not isinstance(prices, list):
                prices = []
            plans = details.get("plans") or []
            if not isinstance(plans, list):
                plans = []

            urls, quote = _source(raw)
            results.append({
                "name": name, "candidate_kind": "unknown", "description": desc,
                "category": category, "key_features": features,
                "main_use_case": main_use_case,
                "pricing_model": pricing_model,
                "target_users": target_users,
                "benefits": benefits,
                "problems_solved": problems_solved,
                "prices": prices[:12],
                "plans": plans[:12],
                "evidence_state": raw.get("evidence_state"),
                "confidence": _confidence(raw), "source_urls": list(urls), "quote": quote,
                "requires_review": True,
            })
    return results


def customer_evidence(report: dict, *, include_inferred: bool = False) -> list[dict]:
    """Extract named customers, case studies, testimonials from the report."""
    idx = _field_index(report)
    results = []
    for key in ("customer", "case_study", "customer_proof", "customer_count"):
        raw = idx.get(key)
        if not raw or not _usable(raw, include_inferred=include_inferred):
            continue
        urls, quote = _source(raw)
        results.append({
            "type": key,
            "value": raw["value"],
            "evidence_state": str(raw.get("evidence_state") or "").upper(),
            "confidence": _confidence(raw),
            "source_urls": list(urls),
            "quote": quote,
        })
    return results


def brand_fields(report: dict, *, include_inferred: bool = False) -> dict[str, Any]:
    """Extract brand positioning fields not covered by value_proposition group."""
    idx = _field_index(report)
    result = {}
    for key in ("mission", "vision", "brand_values", "brand_promise", "brand_personality",
                "calls_to_action", "messaging_consistency", "customer_claims"):
        raw = idx.get(key)
        if not raw or not _usable(raw, include_inferred=include_inferred):
            continue
        result[key] = raw["value"]
    return result


def missing_hints(report: dict) -> list[dict]:
    """Display ZIP gaps, but do not insert them as V1 GapQuestions (different schema)."""
    hints = []
    for section in report.get("sections") or []:
        if not isinstance(section, dict) or section.get("key") != "missing_information":
            continue
        for item in section.get("items") or []:
            if not isinstance(item, dict) or item.get("access", "public") != "public":
                continue
            if not item.get("key") or not item.get("label"):
                continue
            hints.append({"field": item["key"], "label": item["label"],
                          "section": item.get("section"), "reason": item.get("reason")})
    return hints
