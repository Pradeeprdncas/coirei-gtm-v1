from dataclasses import replace

import pytest

from gtm_crawler.config import FrontierSettings
from gtm_crawler.frontier import Frontier, FrontierEntry, Rejection


def entry(
    path: str, *, score: float = 1.0, depth: int = 1, host: str = "example.com"
) -> FrontierEntry:
    return FrontierEntry(
        url=f"https://{host}{path}", depth=depth, score=score, discovered_via="link"
    )


def frontier(**overrides: int) -> Frontier:
    return Frontier("example.com", replace(FrontierSettings(), **overrides))


def test_tracks_queued_urls() -> None:
    f = frontier()
    f.add(entry("/a"))
    assert f.is_queued("https://example.com/a") and len(f) == 1
    f.claimed("https://example.com/a")
    assert not f.is_queued("https://example.com/a") and len(f) == 0


def test_duplicates_rejected_even_after_claim() -> None:
    f = frontier()
    assert f.add(entry("/a")) is None
    assert f.add(entry("/a", score=9)) is Rejection.DUPLICATE
    f.claimed("https://example.com/a")
    assert f.add(entry("/a")) is Rejection.DUPLICATE
    assert f.rejections[Rejection.DUPLICATE] == 2


def test_mark_seen_blocks_later_add() -> None:
    f = frontier()
    assert f.mark_seen("https://example.com/redirect-target") is True
    assert f.mark_seen("https://example.com/redirect-target") is False
    assert f.add(entry("/redirect-target")) is Rejection.DUPLICATE


def test_subdomains_in_scope_other_sites_external() -> None:
    f = frontier()
    assert f.add(entry("/a", host="docs.example.com")) is None
    assert f.add(entry("/a", host="example.org")) is Rejection.EXTERNAL
    assert f.add(entry("/a", host="notexample.com")) is Rejection.EXTERNAL
    assert f.add(entry("/a", host="example.com.evil.net")) is Rejection.EXTERNAL


def test_max_depth_is_retryable_at_shallower_depth() -> None:
    f = frontier(max_depth=2)
    assert f.add(entry("/x", depth=3)) is Rejection.MAX_DEPTH
    assert f.add(entry("/x", depth=2)) is None


def test_restore_rebuilds_admission_state() -> None:
    f = frontier(max_urls_per_prefix=2)
    f.restore("https://example.com/blog/one", queued=False)
    f.restore("https://example.com/blog/two", queued=True)
    assert len(f) == 1 and f.is_queued("https://example.com/blog/two")
    assert f.add(entry("/blog/one")) is Rejection.DUPLICATE
    assert f.add(entry("/blog/three")) is Rejection.PREFIX_BUDGET


@pytest.mark.parametrize(
    ("path", "settings", "reason"),
    [
        ("/" + "a" * 100, {"max_url_length": 50}, Rejection.URL_TOO_LONG),
        ("/1/2/3/4/5", {"max_path_segments": 4}, Rejection.PATH_TOO_DEEP),
        ("/s?a=1&b=2&c=3", {"max_query_params": 2}, Rejection.TOO_MANY_PARAMS),
        ("/x/x/x/x", {}, Rejection.REPEATED_SEGMENTS),
        ("/docs/api/docs/api", {}, Rejection.REPEATED_SEGMENTS),
    ],
)
def test_trap_rules(path: str, settings: dict[str, int], reason: Rejection) -> None:
    assert frontier(**settings).add(entry(path)) is reason


def test_legitimate_repetition_allowed() -> None:
    f = frontier()
    assert f.add(entry("/docs/docs")) is None
    assert f.add(entry("/a/b/c/a")) is None


def test_pattern_budget_caps_calendar_and_facet_traps() -> None:
    f = frontier(max_urls_per_pattern=3)
    results = [f.add(entry(f"/events/2026/{m:02d}")) for m in range(1, 6)]
    assert results[:3] == [None, None, None]
    assert results[3:] == [Rejection.PATTERN_BUDGET] * 2

    facets = [f.add(entry(f"/search?q=term{i}")) for i in range(5)]
    assert facets.count(Rejection.PATTERN_BUDGET) == 2

    # Distinct static pages never share a pattern.
    assert all(f.add(entry(f"/page-{name}")) is None for name in ("about", "team", "press", "jobs"))


def test_prefix_budget_limits_one_section() -> None:
    f = frontier(max_urls_per_prefix=2)
    assert f.add(entry("/blog/one")) is None
    assert f.add(entry("/blog/two")) is None
    assert f.add(entry("/blog/three")) is Rejection.PREFIX_BUDGET
    assert f.add(entry("/pricing")) is None
    # Budgets are per host: a subdomain's /blog is a separate section.
    assert f.add(entry("/blog/one", host="eu.example.com")) is None


def test_queue_full_is_retryable() -> None:
    f = frontier(max_queue_size=1)
    assert f.add(entry("/a")) is None
    assert f.add(entry("/b")) is Rejection.QUEUE_FULL
    f.claimed("https://example.com/a")
    assert f.add(entry("/b")) is None
