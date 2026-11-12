from pathlib import Path

import pytest

from gtm_crawler.config import ConfigError, load_settings


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_defaults_when_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no config.toml here
    settings = load_settings(environ={})
    assert settings.logging.level == "INFO"
    assert settings.database.url.startswith("postgresql+psycopg://")


def test_file_values(tmp_path: Path) -> None:
    path = write(
        tmp_path, '[database]\nurl = "postgresql+psycopg://a@b/c"\n[logging]\nlevel = "DEBUG"\n'
    )
    settings = load_settings(path, environ={})
    assert settings.database.url == "postgresql+psycopg://a@b/c"
    assert settings.logging.level == "DEBUG"


def test_env_overrides_file(tmp_path: Path) -> None:
    path = write(tmp_path, '[logging]\nlevel = "DEBUG"\n')
    settings = load_settings(path, environ={"GTM__LOGGING__LEVEL": "WARNING"})
    assert settings.logging.level == "WARNING"


def test_config_path_from_env(tmp_path: Path) -> None:
    path = write(tmp_path, '[logging]\nlevel = "ERROR"\n')
    assert load_settings(environ={"GTM_CONFIG": str(path)}).logging.level == "ERROR"


def test_explicit_missing_file_is_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "missing.toml", environ={})
    with pytest.raises(ConfigError, match="not found"):
        load_settings(environ={"GTM_CONFIG": str(tmp_path / "missing.toml")})


@pytest.mark.parametrize(
    "body",
    [
        '[logging]\nlevl = "INFO"\n',  # misspelled key
        '[loging]\nlevel = "INFO"\n',  # misspelled section
    ],
)
def test_unknown_keys_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError, match="Unknown config key"):
        load_settings(write(tmp_path, body), environ={})


def test_unknown_env_key_rejected() -> None:
    with pytest.raises(ConfigError, match="Unknown config key"):
        load_settings(path=None, environ={"GTM__DATABASE__URI": "x"})


def test_malformed_env_key_rejected() -> None:
    with pytest.raises(ConfigError, match="expected GTM__"):
        load_settings(path=None, environ={"GTM__DATABASE": "x"})


def test_wrong_type_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="expected str"):
        load_settings(write(tmp_path, "[logging]\nlevel = 3\n"), environ={})


def test_invalid_log_level_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown level"):
        load_settings(write(tmp_path, '[logging]\nlevel = "LOUD"\n'), environ={})


def test_malformed_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, "[logging\n"), environ={})


def test_list_values_from_file_and_env(tmp_path: Path) -> None:
    path = write(tmp_path, '[urls]\nstrip_params = ["utm_*", "ref"]\n')
    assert load_settings(path, environ={}).urls.strip_params == ("utm_*", "ref")
    env = {"GTM__URLS__STRIP_PARAMS": "utm_*, gclid ,"}
    assert load_settings(path, environ=env).urls.strip_params == ("utm_*", "gclid")


def test_list_value_type_checked(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="expected str"):
        load_settings(write(tmp_path, "[urls]\nstrip_params = [1, 2]\n"), environ={})


def test_env_int_and_bool_coercion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    env = {"GTM__FRONTIER__MAX_DEPTH": "3", "GTM__URLS__STRIP_TRAILING_SLASH": "true"}
    settings = load_settings(environ=env)
    assert settings.frontier.max_depth == 3
    assert settings.urls.strip_trailing_slash is True
    with pytest.raises(ConfigError, match="expected int"):
        load_settings(environ={"GTM__FRONTIER__MAX_DEPTH": "deep"})


@pytest.mark.parametrize("body", ["max_depth = -1", "max_queue_size = 0"])
def test_frontier_limits_validated(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError, match="must be positive"):
        load_settings(write(tmp_path, f"[frontier]\n{body}\n"), environ={})


def test_int_list_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_settings(environ={"GTM__FETCH__ALLOWED_PORTS": "443"}).fetch.allowed_ports == (443,)
    with pytest.raises(ConfigError, match="expected int"):
        load_settings(environ={"GTM__FETCH__ALLOWED_PORTS": "443,https"})


def test_string_settings_are_not_range_checked(tmp_path: Path) -> None:
    path = write(tmp_path, '[rawstore]\nroot = "/var/lib/gtm/raw"\nretention_days = 30\n')
    settings = load_settings(path, environ={})
    assert settings.rawstore.root == "/var/lib/gtm/raw"
    with pytest.raises(ConfigError, match="must be positive"):
        load_settings(write(tmp_path, "[parse]\nmax_links = 0\n"), environ={})


def test_profile_overlay_and_env_precedence(tmp_path: Path) -> None:
    base = write(tmp_path, '[logging]\nlevel = "INFO"\n[crawl]\nmax_pages = 10\n')
    (tmp_path / "config.test.toml").write_text('[logging]\nlevel = "WARNING"\n', encoding="utf-8")
    s = load_settings(base, environ={"GTM_ENV": "test"})
    assert s.app.env == "test" and s.logging.level == "WARNING" and s.crawl.max_pages == 10
    s = load_settings(base, environ={"GTM_ENV": "test", "GTM__LOGGING__LEVEL": "ERROR"})
    assert s.logging.level == "ERROR"  # environment beats both files
    assert load_settings(base, environ={}).app.env == "dev"  # overlay only when selected
    with pytest.raises(ConfigError, match="GTM_ENV"):
        load_settings(base, environ={"GTM_ENV": "qa"})


PROD_OK = {
    "GTM_ENV": "prod",
    "GTM__IDENTITY__USER_AGENT_CONTACT": "https://ops.example.com/crawler",
    "GTM__API__TOKEN": "t0ken-for-prod",
    "GTM__DATABASE__URL": "postgresql+psycopg://app:pw@db.internal:5432/gtm",
}


def test_prod_profile_fails_closed(tmp_path: Path) -> None:
    base = write(tmp_path, "")
    with pytest.raises(ConfigError) as exc:
        load_settings(base, environ={"GTM_ENV": "prod"})
    for problem in ("user_agent_contact", "api.token", "development default"):
        assert problem in str(exc.value)
    with pytest.raises(ConfigError, match="DEBUG"):
        load_settings(base, environ={**PROD_OK, "GTM__LOGGING__LEVEL": "DEBUG"})
    assert load_settings(base, environ=PROD_OK).app.env == "prod"


def test_secrets_are_masked_and_kept_out_of_snapshots(tmp_path: Path) -> None:
    from gtm_crawler.config import SearchSettings, Secret, Settings, secret_values
    from gtm_crawler.crawl import config_snapshot

    s = load_settings(write(tmp_path, ""), environ=PROD_OK)
    assert isinstance(s.api.token, Secret) and s.api.token == "t0ken-for-prod"  # noqa: S105
    assert "t0ken" not in repr(s) and "pw@" not in repr(s)
    assert set(secret_values(s)) == {"t0ken-for-prod", PROD_OK["GTM__DATABASE__URL"]}
    # Built in code with a plain string, a credential is still a Secret.
    coded = Settings(search=SearchSettings(api_key="k-123"))
    assert isinstance(coded.search.api_key, Secret)
    snapshot = config_snapshot(coded)
    assert "k-123" not in str(snapshot) and "database" not in snapshot and "api" not in snapshot


def test_feature_flags_must_be_known(tmp_path: Path) -> None:
    assert not load_settings(write(tmp_path, ""), environ={}).feature("anything")
    with pytest.raises(ConfigError, match="unknown feature flag"):
        load_settings(write(tmp_path, '[features]\nenabled = ["typo_flag"]\n'), environ={})


def test_database_limits_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="database.pool_size"):
        load_settings(write(tmp_path, "[database]\npool_size = 0\n"), environ={})
