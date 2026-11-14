# Frontend prompt: product selection → GTM projects → leftover-products screen

This covers the "Which products would you like to take to market?" screen
and what happens after — turning verified products into GTM workspaces
(`Project` rows) via `POST /companies/{id}/projects`, and the follow-up
message for whatever's left over. Builds on
[frontend-verification-flow.md](frontend-verification-flow.md) (editing/
verifying products) and [frontend-gap-questions-flow.md](frontend-gap-questions-flow.md)
(the rest of the onboarding call sequence) — read those first for where this
screen sits in the overall flow.

## 1. Where this screen sits

By the time the user reaches this screen, `POST /companies/{id}/complete`
has already fired automatically — the backend calls it the moment the first
product gets verified (it only ever required *one* verified standalone
product, never all of them), and `current_user_status` is already
`"onboarding_complete"`. This screen isn't part of finishing onboarding; it's
the first thing after it — deciding which verified product(s) actually
become a GTM workspace right now, leaving the rest for later.

If you ever reach this screen a different way and aren't sure onboarding is
complete yet, call `POST /companies/{id}/complete` before `POST
.../projects` — it's a plain no-op if already complete (`{"status":
"onboarding_complete", "changed": false}`), so it's always safe to call.

## 2. Loading the screen

```
GET /companies/{company_id}/products
```

Render every product. Only ones with `status: "verified"` are selectable —
grey out the rest with a "not verified yet" badge rather than hiding them, so
the user sees the full picture (this matches the Figma: showing all products
found, not just the eligible ones).

```json
{ "products": [
  {"id": 12, "name": "Fleet Management Software", "status": "verified",
   "category": "Robotics", "product_type": "Hardware", "description": "..."},
  {"id": 14, "name": "Warehouse Analytics", "status": "detected", ...}
]}
```
(`category`/`product_type` are the two pill tags in the mockup.)

## 3. Creating projects for the selected products

```
POST /companies/{company_id}/projects
{ "product_ids": [12] }
```

- Only `standalone_product` kind, `status: "verified"` products are
  accepted — anything else in the list is rejected wholesale with `409
  PRODUCT_NOT_VERIFIED` and the offending ids in `details.product_ids`, so
  don't let the user check an unverified product's box in the first place.
- Omit `product_ids` (or send `[]`) to create projects for **every** verified
  standalone product at once instead of a hand-picked subset — useful for a
  "create all" shortcut if you want one.
- Calling it again with a product that already has a project is a no-op for
  that product (`existing_project_ids` instead of `created_project_ids`) —
  safe to re-call, nothing duplicates.
- Response:
  ```json
  { "projects": [...], "created_project_ids": [101],
    "existing_project_ids": [], "primary_project_id": 101,
    "redirect_url": "/p/101/home" }
  ```
  With one product selected, just route to `redirect_url`. With several,
  show the leftover screen below first (or route to the first and let them
  switch workspaces from there — your call).

## 4. The leftover-products screen

After the call above, work out what's left over — verified products the
user didn't select, plus anything still unverified:

```js
const projectedIds = new Set([...created_project_ids, ...existing_project_ids]
  .map(pid => /* map project id -> its product_id from the `projects` list in the response */));
const leftover = allProducts.filter(p => !projectedIds.has(p.id));
```

(Simplest in practice: re-`GET /companies/{company_id}/products` and
`GET /companies/{company_id}/projects`, then diff by `product_id` — fewer
assumptions about response ordering.)

If `leftover.length > 0`, show:

> **"{leftover.length} products haven't been verified and don't have a
> project yet."**

with two buttons:

- **Go to verify** — route to the verify screen, scoped to
  `leftover.filter(p => p.status !== 'verified')` (products that are already
  verified but just weren't selected don't need re-verifying — route those
  straight back to this same product-selection screen instead, or fold them
  into a "create project for this one too" shortcut).
- **Complete onboarding** — `onClick` just calls
  `POST /companies/{company_id}/complete` directly, no confirmation dialog
  needed. Since onboarding is already complete by this point (§1), this is
  really a "done, take me to my workspace" action — call it, ignore the
  response beyond a success check, and route to the workspace
  (`redirect_url` from §3, or `GET /companies/{id}/projects` → the first
  project's `/p/{id}/home` if none was captured yet).

If `leftover.length === 0` (everything verified got turned into a project),
skip this screen entirely and route straight to the workspace.
