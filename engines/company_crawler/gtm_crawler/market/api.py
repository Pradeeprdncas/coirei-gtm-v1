"""Market-research endpoints of ``gtm-api`` (mounted by ``api.create_app``).

* ``POST /v1/market/research``: queue a run (a ``ResearchRequest``); a
  ``gtm-worker`` runs it. 201 with its ``research_id``; 429 while
  ``market.max_queued_runs`` are waiting. With an ``Idempotency-Key`` header
  a retry returns the same run (200); the key with another request is a 409.
* ``GET /v1/market/research/{id}``: status, progress, timestamps, and once
  finished the executive summary, gaps and issues.
* ``GET /v1/market/research/{id}/report[?format=markdown]``: the full report
  (sources, evidence, sections, sizing, scores) or its Markdown export.
* ``POST /v1/market/research/{id}/cancel``: cancel a queued run, or ask a
  running one to stop (it keeps what it collected, status CANCELLED).
* ``POST /v1/market/research/{id}/ask``: the run's evidence most relevant to
  a question; with ``market.llm_model`` set, also a model answer whose every
  sentence cites that evidence (labelled model-generated).
* ``POST /v1/market/sizing``: TAM/SAM/SOM from explicit inputs, no research run.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from ..config import Settings
from ..jobs import QueueFull
from .llm import ChatModel, LlmError, retrieve
from .models import Report, ResearchRequest, SizingReport, SizingRequest, new_id, utcnow
from .report import to_markdown
from .sizing import size_market
from .store import FINAL, MarketStore

TAG = "Market research"
Responses = dict[int | str, dict[str, Any]]
NO_RUN: Responses = {404: {"description": "No market research run with this id."}}
TOO_MANY: Responses = {429: {"description": "Too many runs queued; retry after Retry-After."}}


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    k: int = Field(8, ge=1, le=25)


def _terms(req: ResearchRequest) -> dict[str, Any]:
    """What the client asked for (not the id and time the server gave the run)."""
    return req.model_dump(mode="json", exclude={"research_id", "created_at"})


def run_view(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "research_id": row.id, "mode": row.mode, "subject": row.request.get("subject"),
        "status": row.status.upper(), "progress": row.progress, "attempts": row.attempts,
        "cancel_requested": row.cancel_requested, "created_at": row.created_at,
        "started_at": row.started_at, "finished_at": row.finished_at,
        "error_summary": row.error_summary, "verification": row.verification,
    }  # fmt: skip
    if row.report:
        r = row.report
        out["summary"] = {"executive_summary": r.get("executive_summary"), "gaps": r.get("gaps"),
                          "issues": r.get("issues"), "stats": r.get("stats")}  # fmt: skip
    return out


def add_market_routes(app: FastAPI, settings: Settings, engine: Engine,
                      guarded: list[Any]) -> None:  # fmt: skip
    store = MarketStore(engine)

    def row_or_404(research_id: str) -> Any:
        row = store.get(research_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no market research {research_id}")
        return row

    def report_of(research_id: str) -> Report:
        row = row_or_404(research_id)
        if row.report is None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"run is {row.status.upper()}: no report yet")  # fmt: skip
        return Report.model_validate(row.report)

    @app.post("/v1/market/research", dependencies=guarded, tags=[TAG],
              summary="Start market research", status_code=status.HTTP_201_CREATED,
              responses={**TOO_MANY,
                         200: {"description": "The run created earlier with this "
                                              "Idempotency-Key (`created: false`)."},
                         409: {"description": "The Idempotency-Key was used for another "
                                              "request."}})  # fmt: skip
    def create_research(
        body: ResearchRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,
    ) -> dict[str, Any]:
        """Queue an `industry` or `product` research run. The server assigns `research_id`.

        Send an `Idempotency-Key` header to retry safely: the same key with the same
        request returns the run already created (200, `created: false`), never a second
        paid run; with another request it is a 409. Keys do not expire.

        Follow it with `GET /v1/market/research/{research_id}`."""
        req = body.model_copy(update={"research_id": new_id("mr"), "created_at": utcnow()})
        try:
            row = store.create(req, settings.market.max_queued_runs, idempotency_key)
        except QueueFull as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc),
                                headers={"Retry-After": "60"}) from exc  # fmt: skip
        created = row.id == req.research_id
        if not created and _terms(ResearchRequest.model_validate(row.request)) != _terms(req):
            raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency-Key was already used "
                                f"for another request (run {row.id})")  # fmt: skip
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return {**run_view(row), "created": created}

    @app.get("/v1/market/research/{research_id}", dependencies=guarded, tags=[TAG],
             summary="Get a research run", responses=NO_RUN)  # fmt: skip
    def get_research(research_id: str) -> dict[str, Any]:
        """Status (`QUEUED`, `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLED`),
        progress and, once finished, the executive summary, gaps and run issues."""
        return run_view(row_or_404(research_id))

    @app.get("/v1/market/research/{research_id}/report", dependencies=guarded, tags=[TAG],
             summary="Get a research report",
             responses={**NO_RUN, 409: {"description": "The run has no report yet."}})  # fmt: skip
    def get_report(research_id: str, format: str = "json") -> Any:  # noqa: A002 - API name
        """The full report; `?format=markdown` returns `{"markdown": ...}`."""
        report = report_of(research_id)
        if format == "markdown":
            return {"markdown": to_markdown(report)}
        if format != "json":
            raise HTTPException(422, "format must be json or markdown")
        return report.model_dump(mode="json")

    @app.post("/v1/market/research/{research_id}/cancel", dependencies=guarded, tags=[TAG],
              summary="Cancel a research run", status_code=status.HTTP_202_ACCEPTED,
              responses={**NO_RUN, 409: {"description": "The run already finished."}})  # fmt: skip
    def cancel_research(research_id: str, response: Response) -> dict[str, Any]:
        row = row_or_404(research_id)
        if row.status in FINAL:
            raise HTTPException(status.HTTP_409_CONFLICT, f"run is {row.status.upper()}")
        store.cancel(research_id)
        return run_view(row_or_404(research_id))

    @app.post("/v1/market/research/{research_id}/ask", dependencies=guarded, tags=[TAG],
              summary="Ask a question of a run's evidence",
              responses={**NO_RUN, 409: {"description": "The run has no report yet."}})  # fmt: skip
    async def ask(research_id: str, body: Question) -> dict[str, Any]:
        """The excerpts most relevant to the question (TF-IDF over this run's evidence)
        and, when a model is configured, an answer citing them (model-generated)."""
        report = report_of(research_id)
        hits = retrieve(report.evidence, body.question, body.k)
        sources = {s.source_id: s for s in report.sources}
        out: dict[str, Any] = {
            "question": body.question,
            "evidence": [{"evidence_id": e.evidence_id, "score": score, "excerpt": e.excerpt,
                          "url": sources[e.source_id].url if e.source_id in sources else None,
                          "page": e.locator.page if e.locator else None,
                          "dimensions": e.dimensions} for e, score in hits],
            "answer": None,
        }  # fmt: skip
        if settings.market.llm_model and hits:
            model = ChatModel(settings.market)
            try:
                text, cited = await model.answer(body.question, [e for e, _ in hits])
                out["answer"] = {
                    "text": text,
                    "evidence_ids": cited,
                    "basis": "model_generated",
                    "model": settings.market.llm_model,
                }
            except LlmError as exc:
                out["answer_error"] = str(exc)
            finally:
                await model.aclose()
        return out

    @app.post("/v1/market/sizing", dependencies=guarded, tags=[TAG],
              summary="Size a market from explicit inputs")  # fmt: skip
    def sizing(body: SizingRequest) -> SizingReport:
        """TAM/SAM/SOM with scenarios and sensitivity from the given inputs only
        (no research run, so no collected total and nothing counts as corroborated)."""
        return size_market(body.model_copy(update={"use_evidence_total": False}), [],
                           lambda _ids: False)  # fmt: skip
