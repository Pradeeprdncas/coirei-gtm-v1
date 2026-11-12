import asyncio
import io
import json
import logging

from gtm_crawler.config import LoggingSettings
from gtm_crawler.log import configure_logging, log_context


def test_emits_one_json_object_per_record_with_extra_fields() -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="DEBUG"), stream=stream)
    log = logging.getLogger("gtm_crawler.test")

    log.info("fetch.completed", extra={"url": "https://example.com/", "status": 200})
    log.debug("url.skipped", extra={"reason": "robots"})

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [e["event"] for e in lines] == ["fetch.completed", "url.skipped"]
    assert lines[0]["url"] == "https://example.com/"
    assert lines[0]["status"] == 200
    assert lines[0]["level"] == "INFO"
    assert lines[0]["logger"] == "gtm_crawler.test"
    assert "ts" in lines[0]
    # Standard LogRecord attributes must not leak into the output.
    assert "pathname" not in lines[0] and "args" not in lines[0]


def test_level_filters_and_exceptions_serialized() -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="WARNING"), stream=stream)
    log = logging.getLogger("gtm_crawler.test")

    log.info("hidden")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("fetch.error")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(lines) == 1
    assert "RuntimeError: boom" in lines[0]["exc"]


def test_log_context_correlates_tasks_and_threads() -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="INFO"), stream=stream)
    log = logging.getLogger("gtm_crawler.test")

    async def run() -> None:
        with log_context(job_id=7):
            with log_context(crawl_id=42):
                await asyncio.create_task(asyncio.to_thread(log.info, "in.thread"))
            log.info("outer")
        log.info("outside")

    asyncio.run(run())
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(e["event"], e.get("job_id"), e.get("crawl_id")) for e in lines] == [
        ("in.thread", 7, 42),
        ("outer", 7, None),
        ("outside", None, None),
    ]


def test_secrets_and_credentials_are_redacted() -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="INFO"), stream=stream, secrets=["exa-key-123"])
    log = logging.getLogger("gtm_crawler.test")
    log.info("search.failed", extra={"error": "HTTP 401: bad key exa-key-123"})
    log.info("db.error", extra={"url": "postgresql+psycopg://app:hunter2@db:5432/gtm"})
    log.info("api.request", extra={"header": "Authorization: Bearer abc.def-ghi"})
    out = stream.getvalue()
    assert "exa-key-123" not in out and "hunter2" not in out and "abc.def" not in out
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[1]["url"] == "postgresql+psycopg://***:***@db:5432/gtm"
    assert lines[2]["header"] == "Authorization: Bearer ***"


def test_untrusted_content_cannot_forge_log_lines() -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="INFO"), stream=stream)
    log = logging.getLogger("gtm_crawler.test")
    hostile = 'x"}\n{"level": "ERROR", "event": "forged"}  \x1b[31m'
    log.info("page.title", extra={"title": hostile})
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["title"] == hostile
