"""Identity evaluation corpus: labelled cases scored as a whole.

A false merge (a MATCH where the label says otherwise) is the failure that
puts one company's facts on another, so it must be zero; the other rates are
reported by the assertion message when they slip.
"""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import Connection, text

from gtm_crawler.db.evidence import _org_by_name
from gtm_crawler.db.identity import resolve_member, resolve_profiles

from .test_identity_resolution import LINKEDIN, _crawl, _page, _signal

pytestmark = pytest.mark.db


def _member(candidates: dict[str, list[dict[str, Any]]]) -> Callable[[Connection], str]:
    def run(conn: Connection) -> str:
        acme, _ = _crawl(conn, "acme.com")
        return resolve_member(conn, _org_by_name(conn, "Globex", acme), candidates).outcome

    return run


def _profile(*pages: tuple[str, str], rival: bool = False) -> Callable[[Connection], str]:
    """Outcome for acme.com's LinkedIn profile given ``pages`` (page URL, link target)."""

    def run(conn: Connection) -> str:
        if rival:  # another company with a reciprocal claim on the same profile
            _, other = _crawl(conn, "acme.io")
            _page(conn, other, "https://acme.io/", LINKEDIN)
            _page(conn, other, LINKEDIN, "https://acme.io/")
            resolve_profiles(conn, other)
        _, crawl = _crawl(conn, "acme.com")
        for url, target in pages:
            _page(conn, crawl, url, target)
        return next(iter(resolve_profiles(conn, crawl)), "none")

    return run


SITE = "https://acme.com/"
CASES: list[tuple[str, str, Callable[[Connection], str]]] = [
    ("name links to one site", "match", _member({"globex.com": [_signal()]})),
    ("name, no link on the page", "unknown", _member({})),
    ("same name, two candidate sites", "conflicting",
     _member({"globex.com": [_signal(1)], "globex.io": [_signal(2)]})),
    ("profile and site link each other", "match", _profile((SITE, LINKEDIN), (LINKEDIN, SITE))),
    ("site links profile only", "probable_match", _profile((SITE, LINKEDIN))),
    ("profile links site only", "probable_match", _profile((LINKEDIN, SITE))),
    ("same-name profile, no link either way", "unknown",
     _profile((LINKEDIN, "https://acme.co.uk/"))),
    ("profile verified by another company too", "conflicting",
     _profile((SITE, LINKEDIN), (LINKEDIN, SITE), rival=True)),
    ("one-way claim on another company's verified profile", "conflicting",
     _profile((SITE, LINKEDIN), rival=True)),
    ("link to a person, not a company profile", "none",
     _profile((SITE, "https://www.linkedin.com/in/jane-doe"))),
]  # fmt: skip


def test_identity_corpus(conn: Connection) -> None:
    conn.execute(text("TRUNCATE identity_link, identity_decision, external_identity CASCADE"))
    got: dict[str, str] = {}
    for name, _, run in CASES:
        case = conn.begin_nested()  # each case alone
        got[name] = run(conn)
        case.rollback()
    wrong = {n: (want, got[n]) for n, want, _ in CASES if got[n] != want}
    false_merges = [n for n, want, _ in CASES if got[n] == "match" and want != "match"]
    assert false_merges == [], f"false merges: {false_merges}"
    for outcome in ("match", "unknown", "conflicting"):
        labelled = [n for n, want, _ in CASES if want == outcome]
        assert all(got[n] == outcome for n in labelled), f"{outcome} missed: {wrong}"
    assert wrong == {}, wrong
