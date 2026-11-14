# Frontend prompt: editing, verifying, and completing onboarding

This covers `PATCH /products/{product_id}`, `POST /products/{product_id}/verify`,
and `POST /companies/{company_id}/complete` — where every field comes from, how
editing works (pricing included), and the `revision` discipline that makes all
three safe to call from a UI that lets people edit freely.

## 1. Where the fields come from

Call `GET /products/{product_id}` before showing an edit/verify screen. It
returns:

```json
{
  "id": 12, "company_id": 4, "name": "...", "website_url": "...",
  "candidate_kind": "standalone_product", "category": "...", "product_type": "...",
  "description": "...", "key_features": ["..."], "main_use_case": "...",
  "business_model": {...}, "pricing_status": "published_needs_review",
  "positioning": "...", "value_proposition": {...}, "target_market": {...},
  "icp": {...}, "field_meta": { "<field>": {"status","review","confidence","source_id","quote","via"} },
  "status": "detected", "revision": 3, "verified_at": null,
  "pricing": [
    {"id": 7, "name": "Starter", "price_amount": 29.99, "currency": "USD",
     "unit": null, "period": "month", "is_custom": false, "quote": null, "source_id": null}
  ],
  "evidence": [ {"id","field","value","source_id","quote","status"} ],
  "verification": {"can_verify": false, "blocking_reasons": ["missing_key_features"]}
}
```

- `pricing` is a **separate list**, not a field on the product object — it comes
  from its own `pricing_items` table, which is why it's returned alongside the
  product rather than inside it.
- `field_meta[<field>]` tells you whether a field is still AI-extracted
  (`review` not `"accepted"`) or already user-confirmed (`review: "accepted"`).
  Use this to decide which fields show a "Review" vs "Edit" affordance.
- `verification.blocking_reasons` tells you up front whether `verify` will
  succeed, and why not if it won't — use it to enable/disable the verify
  button without having to call verify speculatively.

For the company-level equivalent, `GET /companies/{company_id}/profile` works
the same way and feeds `PATCH /companies/{company_id}/profile`.

## 2. Editing (`PATCH /products/{product_id}`)

```json
{
  "revision": 3,
  "fields": { "description": "...", "pricing": [ {"name": "Starter", "price_amount": 29.99, "currency": "USD", "period": "month"} ] },
  "accept_fields": []
}
```

- `fields` is a generic bag: send only the keys you're changing. Editable keys:
  `name, website_url, candidate_kind, category, product_type, description,
  key_features, main_use_case, business_model, pricing_status, positioning,
  value_proposition, target_market, icp, pricing`. Anything else is rejected
  with `422 UNKNOWN_FIELD`.
- **Pricing is fully editable now.** Send `fields.pricing` as a list of plan
  objects and the server replaces every pricing row for that product in one
  shot — this is a full-list replace, not a per-item patch, so always send the
  complete pricing list you want to end up with (including untouched plans).
  Only `name` is required per item; everything else is optional and defaults
  sensibly:
  ```json
  { "name": "Enterprise", "price_amount": null, "currency": null,
    "unit": null, "period": null, "is_custom": true, "quote": "contact sales" }
  ```
  `currency` must be a 3-letter code if present (case-insensitive, normalized
  to upper). `price_amount` must be numeric if present. Since pricing isn't a
  JSONB blob but real rows, you can't `accept_fields: ["pricing"]` — there's no
  AI-review state to accept, you just set the value directly. Any other field
  that's a JSON object (`business_model`, `value_proposition`, `target_market`,
  `icp`) really is a JSONB column, so you can post any JSON shape for those —
  no fixed schema is enforced beyond "must be an object".
- `accept_fields`: list of field names to mark `review: "accepted"` in
  `field_meta` without changing their value — this is how the frontend
  confirms an AI-extracted value is correct as-is (used for the
  `review_<field>` blockers `verify` reports). A field can't be accepted while
  empty (`409 EMPTY_FIELD`).
- Editing **any** field on an already-verified product resets `status` back
  to `"detected"` and clears `verified_at` — verification isn't sticky across
  edits, the user has to re-verify after changing anything.
- Response: `{"product": {...same shape as GET...}, "changed": bool, "verification": {"can_verify","blocking_reasons"}}`.
  Always read `product.revision` off this response and use it for your next
  call — see §4.

## 3. Verifying (`POST /products/{product_id}/verify`)

```json
{ "revision": 3, "status": "verified", "accept_fields": ["key_features"] }
```

- `accept_fields` here works the same as in PATCH, applied in the same call as
  verification — so a single verify call can both accept outstanding
  AI-reviewed fields and flip the product to verified, as long as nothing is
  still missing outright (missing data and `not_standalone_product`/
  `invalid_product_name` can't be resolved by accepting — only by editing
  first).
- `409 PRODUCT_REVIEW_INCOMPLETE` if blockers remain after applying
  `accept_fields`; the error body lists `blocking_reasons` and
  `required_accept_fields` so you can show exactly what's missing.
- Calling verify again on an already-verified product with the SAME revision
  it returned, or the revision just before it, is treated as an idempotent
  no-op (`changed: false`) rather than a stale-revision error — this only
  covers an accidental double-click/retry of the verify call itself, not a
  license to skip re-fetching after a real edit.
- Response includes `all_products_verified` — true once every standalone
  product on the company is verified. When it flips to true, the server
  automatically posts a `company_profile` message into the onboarding
  conversation (fetch it via `GET /companies/{id}/conversation` to render the
  "confirm & go to workspace" screen) — the frontend doesn't need to assemble
  that summary itself.

## 4. The `revision` discipline

Every mutating call on `Company`/`Product` takes the caller's `revision` and
compares it to the current DB value:

- **Exact match required** on `PATCH /products/{id}` and
  `PATCH /companies/{id}/profile` — any mismatch is `409 STALE_REVISION` with
  the current `revision` in the error body, no exceptions.
- **One-step forgiving** on `POST /products/{id}/verify`, but only when the
  product is already `verified` — a plain safety net for the verify request
  itself being retried, not a general grace period.

Rule for the frontend: **always use the `revision` from the most recent
response for that object** (PATCH's `product.revision`, verify's `revision`,
or the initial GET's `revision`) as the `revision` you send on the next call —
never a value cached from earlier in the session. If a user has the edit
screen open for a while, re-`GET` before letting them submit, since another
tab/device or an automated re-extraction could have bumped it. On `409
STALE_REVISION`, re-fetch and show the fresh values (don't silently retry with
a guessed revision) — the whole point of the lock is to stop a user from
overwriting a change they haven't seen.

This is the same pattern across the company profile and both product
endpoints, so one re-fetch-before-edit convention in the frontend covers all
three.

## 5. Completing onboarding (`POST /companies/{company_id}/complete`)

No body needed. Requires at least one verified standalone product
(`409 NO_VERIFIED_PRODUCT` otherwise, with a message telling the user to
verify a product first). On success:

```json
{ "company_id": 4, "status": "onboarding_complete", "verified_product_id": 12,
  "next_action": "create_projects", "conversation_url": "..." }
```

- `status` is `"onboarding_complete"` (not `"complete"`) — this matches the
  `current_user_status` field now returned from login/signup, which also uses
  `"onboarding_complete"` for this stage. Completing onboarding doesn't mean
  the whole project is done — competitor discovery and later steps are
  separate, future milestones — so the name deliberately doesn't claim more
  than it does. Calling this endpoint again once already complete is a
  no-op that returns the same `status: "onboarding_complete"`.
- A `onboarding_complete` message is also posted into the conversation, and
  `company.status` moves out of the onboarding flow entirely, so this is the
  signal to route the user to the main workspace (`next_action:
  "create_projects"`).
