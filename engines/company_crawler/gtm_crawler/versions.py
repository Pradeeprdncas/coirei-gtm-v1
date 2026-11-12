"""Versions of everything that affects factual output (spec section 24).

Recorded with every crawl (``crawl.versions``) and on the job each time a
worker claims it (``crawl_job.versions``), so any stored result can be traced
to the code and configuration that produced it. Observations also carry
their extractor's version, pages their parser version, and assessments their
method version.

* ``crawler``: the package version.
* ``parser``, ``extractors``, ``assessment``: bumped by hand when output for
  the same input changes.
* ``dossier_schema``: the dossier document's shape.
* ``quality_report``: the crawl-quality report's definitions (``quality.py``).
* ``artifact``: the raw-store format (``rawstore.ARTIFACT_VERSION``).
* ``search_templates`` and ``config``: content hashes, so they change exactly
  when the query templates or the output-affecting configuration change.
  ``config`` hashes the crawl's configuration snapshot without the settings
  that cannot change a fact (``NOT_OUTPUT``): the environment name, where raw
  bodies are stored, logging, prices and the operator's contact address. So
  staging and production with the same budgets, or two hosts with different
  raw-store paths, record the same hash (Phase 18 fix).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import __version__
from .config import Settings
from .rawstore import ARTIFACT_VERSION

DOSSIER_SCHEMA_VERSION = "7"
# Configuration that does not affect factual output: whole sections (None) or keys.
NOT_OUTPUT: dict[str, tuple[str, ...] | None] = {
    "app": None, "logging": None, "rawstore": None, "cost": None, "market": None,
    "identity": ("user_agent_contact",),
}  # fmt: skip


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=_jsonable)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _jsonable(value: Any) -> Any:
    if isinstance(value, frozenset | set):
        return sorted(value)
    return str(value)


def config_hash(snapshot: dict[str, Any]) -> str:
    """Hash of the output-affecting part of a configuration snapshot
    (``crawl.config_snapshot``, or the ``crawl.config`` column as stored)."""
    kept = {}
    for section, values in snapshot.items():
        drop = NOT_OUTPUT.get(section, ())
        if drop is not None:
            kept[section] = {k: v for k, v in values.items() if k not in drop}
    return _digest(kept)


def current_versions(settings: Settings) -> dict[str, Any]:
    # Imported here: these modules import much of the package.
    from .assess import method_version
    from .crawl import SEARCH_PLAN, config_snapshot
    from .extract import EXTRACTOR_VERSIONS
    from .parse import PARSER_VERSION
    from .quality import QUALITY_REPORT_VERSION
    from .search import COMPETITOR_TEMPLATES

    return {
        "crawler": __version__,
        "parser": PARSER_VERSION,
        "artifact": ARTIFACT_VERSION,
        "extractors": dict(EXTRACTOR_VERSIONS),
        "assessment": method_version(settings.assessment),
        "dossier_schema": DOSSIER_SCHEMA_VERSION,
        "quality_report": QUALITY_REPORT_VERSION,
        "search_templates": _digest([SEARCH_PLAN, COMPETITOR_TEMPLATES]),
        "config": config_hash(config_snapshot(settings)),
    }
