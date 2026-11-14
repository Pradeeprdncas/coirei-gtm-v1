# Product & verification API — exact UI field binding

The verification pages bind to fixed names (`icp.targetAudience`,
`pricing.plans[].billingPeriod`, …) while extraction stores free-form JSONB
whose keys vary by whichever extractor filled them. `app/services/presentation.py`
maps between the two, so correctly-extracted data stops showing as
"Not provided" just because the extractor named it `target_audience`.

**No field inside a spec section is ever `null`** — blanks are `""` / `[]`,
and every section carries `status` + `confidence` so the UI can tell
"not found yet" from "genuinely absent".

## Per-field status vocabulary (every detail endpoint)

| `status` | Meaning |
|---|---|
| `verified` | User accepted it (`review: "accepted"`) |
| `extracted` | Found on the site, confidence ≥ 70 |
| `needs_confirmation` | Found on the site, confidence < 70 |
| `inferred` | AI-synthesized, not quoted from a source |
| `not_found` | No value |

`confidence` is 0-100. When extraction recorded no entry it falls back to the
score its status implies (verified 100 / extracted 75 / needs_confirmation 50 /
inferred 50), so a populated field never reads as 0%.

## `GET /products/{product_id}`

`product_id` is the **numeric id** (from the products list). Passing the
product *name* returns `422` — that is FastAPI validating the path type, not a
backend bug.

```jsonc
{ "success": true, "status": "success", "data": {
  "id": 12, "name": "Cartesia",
  "avatarLetter": "C", "domain": "cartesia.ai",
  "status": "detected",                 // raw DB state
  "review_status": "needs_verification",// hero pill: found | needs_verification | verified
  "revision": 1, "website_url": "...", "last_updated": "14 Nov 2026, 12:33 PM",

  "overview": { "status": "verified", "confidence": 90, "productName": "...",
                "category": "", "productType": [], "businessModel": ["SaaS Subscription"],
                "website": "...", "description": "..." },

  "key_features": { "status": "needs_confirmation", "confidence": 60, "items": ["..."] },

  "value_proposition": { "status": "extracted", "confidence": 75,
                         "aiInsight": "...", "keyBenefits": ["..."], "additional": {} },

  "target_market": { "status": "...", "confidence": 75, "segment": "", "industries": [],
                     "buyers": "", "region": "", "painPoints": [], "reachable": "",
                     "additional": {} },

  "icp": { "status": "inferred", "confidence": 50, "targetAudience": "", "industry": "...",
           "companySize": "...", "geography": "", "decisionMakers": "",
           "customerPainPoints": "",
           "additional": { "compliance_needs": "HIPAA, SOC 2, GDPR" } },

  "positioning": { "status": "extracted", "confidence": 75, "statement": "..." },

  "pricing": { "status": "extracted", "confidence": 75, "pricingModel": "SaaS Subscription",
               "billingCycle": "Month & Year", "currency": "USD",
               "plans": [ { "id": 21, "name": "Starting at", "price": "$36,000",
                            "billingPeriod": "/year", "features": "Starting at $36,000 / yr",
                            "currency": "USD", "is_custom": false, "price_amount": 36000.0 } ] },

  "field_meta": { "description": { "source": "https://…/pricing", "source_id": 64,
                                   "score": 90, "review": "accepted",
                                   "quote": "…", "status": "found_on_site" } },

  "field_status": { "description": "verified", "icp": "inferred", "…": "…" },
  "verification": { "can_verify": false, "blocking_reasons": ["review_main_use_case"] },
  "evidence": [ { "id": 141, "field": "name", "value": {"value":"Cartesia"},
                  "source_id": 71, "source": "https://…", "quote": "Cartesia",
                  "status": "evidence" } ]
}}
```

### `additional` — why it exists
AI and the GTM crawler emit keys with no fixed UI slot (`compliance_needs`,
`voice_call_volume`, `technology_maturity`). Rendering only the fixed names
would silently drop them, so each section returns the leftovers in
`additional` as `{label: value}`. Render them as extra rows.

### `*_raw` — for round-tripping edits
`key_features_raw` (list), `value_proposition_raw`, `target_market_raw`,
`icp_raw`, `positioning_raw`, `pricing_raw`, `field_meta_raw` carry the
unmapped DB values. **PATCH sends the raw shape** (`key_features` is a plain
list on the way in), so edit forms should submit from `*_raw`.

## `GET /companies/{company_id}/products`

`products[]` is the grid card; `products_detail[]` is the full objects for the
switcher/breadcrumb.

```jsonc
{ "data": { "products": [ {
  "id": 43, "name": "Cartesia", "avatarLetter": "C", "domain": "cartesia.ai",
  "status": "needs_verification",          // found | verified | needs_verification
  "description": "...",
  "tags": ["Healthcare", "Fintech", "Regulated sectors"],
  "price": "$36,000", "billingPeriod": "/year", "model": "SaaS Subscription",
  "verification": {...}, "field_status": {...},
  "revision": 1, "website_url": "...", "candidate_kind": "standalone_product",
  "product_status": "detected"
} ] } }
```

`tags` come from real classification only — category, product_type, target
industries, ICP industry, business model. Plan names (Free/Pro/Enterprise)
are packaging, not tags, so they are excluded.

## Gap questions — answer shapes

Each question in `POST /companies/{id}/gap-questions` now carries how to
render it:

```jsonc
{ "id": 7, "field": "company.business_model", "question": "How do you charge customers?",
  "type": "choice",              // multi_choice | choice | text
  "options": [ { "id": "opt-1", "label": "Subscription billing",
                 "value": "Subscription billing", "source": "evidence",
                 "source_url": "https://…/pricing", "quote": "billed monthly",
                 "verified_by": "ai" } ],
  "allow_custom_answer": true, "status": "needs_answer", "answer": null,
  "suggestions": [ … ] }
```

- `multi_choice` — list-valued fields (`key_features`, the product catalogue)
- `choice` — evidence-backed suggestions exist, pick one
- `text` — no suggestions to offer, free text

`allow_custom_answer` is always true: the user can type over any option.
