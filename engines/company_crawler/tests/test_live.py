"""The live view: the pages a crawl fetched most recently, each as a screenshot of the
stored copy (JavaScript off, the site not fetched again)."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from gtm_crawler.db.models import CrawlJob

from .test_crawl import settings_for
from .test_onboarding_api import crawled


@pytest.mark.db
def test_live_lists_the_latest_pages_and_shoots_each(engine: Engine, tmp_path: Path) -> None:
    api, company = crawled(engine, settings_for(tmp_path), "bip-live")
    with engine.connect() as conn:
        job = conn.execute(select(CrawlJob.id).where(CrawlJob.organization_id == company)
                           .order_by(CrawlJob.id.desc())).scalar()  # fmt: skip
    live = api.get(f"/v1/crawls/{job}/live", params={"limit": 3}).json()
    assert live["status"] == "COMPLETED" and 1 <= len(live["pages"]) <= 3
    newest = live["pages"][0]
    assert newest["url"].startswith("https://bip-live.example/")
    ids = [p["fetch_id"] for p in live["pages"]]
    assert ids == sorted(ids, reverse=True)  # newest first

    shot = api.get(newest["screenshot"])
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/png"
    assert shot.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert api.get(newest["screenshot"]).content == shot.content  # kept, not rendered again
    # Only the job's own pages; an unknown crawl is 404.
    assert api.get(f"/v1/crawls/{job}/live/999999999.png").status_code == 404
    assert api.get("/v1/crawls/999999999/live").status_code == 404
