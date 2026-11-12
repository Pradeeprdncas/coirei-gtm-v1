"""Market intelligence: industry- and product-level research on top of the crawler.

The pipeline (``engine.run_research``) plans search queries for a research
request, discovers candidate pages, fetches each as a single page under the
crawler's own safeguards (network guard, robots.txt, per-host throttle),
extracts quoted evidence with rules (no LLM), assesses source quality and
contradictions, and builds a structured, source-linked report. Market sizing
(``sizing.py``) and opportunity scoring (``scoring.py``) are deterministic
calculations over explicit inputs. See docs/MARKET_ENGINE_IMPLEMENTATION.md.
"""
