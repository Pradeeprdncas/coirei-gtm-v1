# Frontend prompt: live progress feed during research (add_context + intelligence crawl)

This replaces the old event-stream (SSE) style status you had, with a polling
endpoint that gives the same feel — a steadily growing feed of what's
happening, a live screenshot that changes every several seconds, and a clear
success/failure at the end — without needing SSE/push infrastructure. There's
no push architecture in this backend (no webhooks, no durable job queue with
subscribers) — everything is discovered by polling, and this endpoint is
built to make that polling feel live rather than like a dumb spinner.

## Why polling instead of true SSE

The old API streamed `thinking`/`browsing`/`searching` events as they
happened. This backend runs two independent async pipelines per company —
the lightweight `add_context` extraction job (in-process) and the GTM
crawler's deep crawl (a separate service) — and neither pushes updates
anywhere. Rebuilding that as true server-sent events would mean adding
streaming infrastructure neither pipeline has today. Polling one endpoint
every few seconds gets you the identical user-facing result (a feed that
keeps growing, screenshots that keep changing, a final success/failure) with
none of that new infrastructure, so that's the model this endpoint follows.

## The endpoint

```
GET /companies/{company_id}/status
```

Poll it every **`poll_interval_seconds`** from the response (currently `7`
— use the field, don't hardcode a number, in case it's tuned later). Keep
polling until `overall_status` is `"success"` or `"failed"`.

Key fields:

- **`overall_status`**: `"not_started" | "running" | "success" | "failed"` —
  combines both pipelines (add_context and the GTM crawl, if one was
  started): either still running keeps it `"running"`; either failing makes
  it `"failed"`; otherwise `"success"` once at least one has completed. This
  is your stop-polling signal.
- **`steps`**: an array that only ever grows, in chronological order —
  render it as the scrolling feed, the same way you'd render the old SSE
  events. Each entry:
  ```json
  { "kind": "browsing", "at": "2026-11-14T10:00:05+00:00",
    "url": "https://example.com/pricing", "status": "Read 1 page(s)",
    "screenshot_url": "/companies/42/intelligence/live-screenshot/931" }
  ```
  `kind` is one of `thinking`, `browsing`, `success`, `failed`. Not every
  entry has a `url`/`screenshot_url` (the add_context pipeline's own
  `thinking` steps don't; GTM crawler page-fetch entries do). A `success` or
  `failed` entry is always appended last, once `overall_status` settles —
  that's your "research complete" / "research failed" moment. Diff against
  what you already rendered rather than re-rendering the whole list each
  poll (entries never change or get removed, only appended).
- **`live_browser_url`**: the single current "what's on screen right now"
  screenshot — use this for the main live-preview pane, not individual
  `steps[].screenshot_url` entries (those are for an optional expandable
  history/timeline view). It's already debounced server-side to hold the
  same screenshot for about 10 seconds before switching to a newer one, so
  the view doesn't flicker on every poll — just `<img src>` it directly
  on each poll's response, no client-side timing needed.
- **`messages`**: a short human-readable summary of current state (1-3
  strings) — good for a one-line status caption above the feed, separate
  from the full `steps` history.
- `warnings`, `error`: as before, for surfacing degraded-but-not-failed runs
  (e.g. AI extraction unavailable but scraping succeeded) or the failure
  reason.

## Suggested UI loop

```js
async function poll(companyId) {
  const { data } = await getStatus(companyId);
  renderLiveScreenshot(data.live_browser_url);
  appendNewSteps(data.steps);       // only render entries not already shown
  renderStatusLine(data.messages);
  if (data.overall_status === 'success' || data.overall_status === 'failed') {
    stopPolling();
    return showOutcome(data.overall_status, data.messages);
  }
  setTimeout(() => poll(companyId), data.poll_interval_seconds * 1000);
}
```

That's the whole loop — one endpoint, no stream to keep open, no reconnect
logic, and it degrades gracefully (a missed poll just means the next one
catches up, since `steps` always contains full history, not just a delta).
