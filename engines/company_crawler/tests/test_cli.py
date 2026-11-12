"""CLI error paths (a full run needs the network; see test_truthfulness for runs)."""

from pathlib import Path

import pytest

from gtm_crawler.cli import main


def test_missing_contact_is_a_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[identity]\nuser_agent_contact = ""\n')
    monkeypatch.setenv("GTM__DATABASE__URL", "postgresql+psycopg://x:y@127.0.0.1:1/none")
    assert main(["https://example.com", "--config", str(config)]) == 2
    assert "user_agent_contact" in capsys.readouterr().err


def test_invalid_url_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[identity]\nuser_agent_contact = "ops@example.com"\n')
    assert main(["ftp://example.com/", "--config", str(config)]) == 2
    assert "error:" in capsys.readouterr().err
