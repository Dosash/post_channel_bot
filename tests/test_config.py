"""Configuration (`app/config.py`): WEB_PREFIX / HUB_URL normalization, the panel URL, version."""
from __future__ import annotations

from pathlib import Path

import pytest

import app
from app.config import Config, load_config, normalize_hub_url, normalize_prefix

ENV_KEYS = ("BOT_TOKEN", "ADMIN_IDS", "WEB_HOST", "WEB_PORT", "WEB_PUBLIC_URL", "WEB_PASSWORD", "WEB_PREFIX",
            "HUB_URL", "TIMEZONE", "DB_PATH", "UPLOADS_DIR", "LOG_LEVEL")


@pytest.mark.parametrize("raw, expected", [
    ("", ""), ("/", ""), ("//", ""), ("  ", ""), (None, ""),
    ("posts", "/posts"), ("/posts", "/posts"), ("/posts/", "/posts"), ("posts/", "/posts"),
    (" /posts/ ", "/posts"), ("//posts//", "/posts"),
    ("tools/posts", "/tools/posts"), ("/tools//posts/", "/tools/posts"),
    ("my-panel_2.v~1", "/my-panel_2.v~1"),
])
def test_normalize_prefix(raw, expected):
    assert normalize_prefix(raw) == expected


@pytest.mark.parametrize("raw", ["/po sts", "/posts?x=1", "/posts#a", "/../etc", "/./posts", "/постs",
                                 "/a%2Fb", '/"x"', "/<x>"])
def test_normalize_prefix_rejects_bad_paths(raw):
    with pytest.raises(ValueError, match="WEB_PREFIX"):
        normalize_prefix(raw)


@pytest.mark.parametrize("raw, expected", [
    ("", None), ("   ", None), (None, None),
    ("/", "/"), (" /hub/ ", "/hub/"), ("https://panels.example.com/", "https://panels.example.com/"),
    ("http://10.0.0.1/", "http://10.0.0.1/"),
])
def test_normalize_hub_url(raw, expected):
    assert normalize_hub_url(raw) == expected


@pytest.mark.parametrize("raw", ["javascript:alert(1)", "//evil.example.com/", "hub", "ftp://x/"])
def test_normalize_hub_url_rejects(raw):
    with pytest.raises(ValueError, match="HUB_URL"):
        normalize_hub_url(raw)


def test_config_defaults_and_version():
    cfg = Config(bot_token="1:x")
    assert cfg.web_prefix == "" and cfg.hub_url is None
    assert cfg.version == app.__version__ == "2.3.0"
    assert cfg.web_url == "http://127.0.0.1:8080/"


def test_config_normalizes_on_construction():
    cfg = Config(bot_token="1:x", web_prefix="posts/", hub_url=" / ")
    assert cfg.web_prefix == "/posts" and cfg.hub_url == "/"
    with pytest.raises(ValueError):
        Config(bot_token="1:x", web_prefix="a b")


@pytest.mark.parametrize("host, port, prefix, expected", [
    ("127.0.0.1", 8080, "", "http://127.0.0.1:8080/"),
    ("0.0.0.0", 8095, "/posts", "http://127.0.0.1:8095/posts/"),
    ("::", 8095, "posts", "http://127.0.0.1:8095/posts/"),
    ("::1", 8080, "", "http://[::1]:8080/"),
    ("10.0.0.5", 9000, "/a/b", "http://10.0.0.5:9000/a/b/"),
])
def test_web_url_local(host, port, prefix, expected):
    assert Config(bot_token="1:x", web_host=host, web_port=port, web_prefix=prefix).web_url == expected


def test_web_url_public_is_used_as_given():
    cfg = Config(bot_token="1:x", web_prefix="/posts", web_public_url="https://panels.example.com/posts/")
    assert cfg.web_url == "https://panels.example.com/posts/"
    cfg.web_public_url = "https://posts.example.com"
    assert cfg.web_url == "https://posts.example.com"


def test_web_url_follows_a_prefix_set_later():
    cfg = Config(bot_token="1:x")
    cfg.web_prefix = "posts/"  # not normalized: web_url still builds a clean URL
    assert cfg.web_url == "http://127.0.0.1:8080/posts/"


# ------------------------------------------------------------------ load_config

@pytest.fixture
def env(monkeypatch, tmp_path):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "bot.db"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "data" / "uploads"))
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    return monkeypatch, env_file


def test_load_config_new_variables(env):
    monkeypatch, env_file = env
    monkeypatch.setenv("WEB_PREFIX", "/posts/")
    monkeypatch.setenv("HUB_URL", "/")
    monkeypatch.setenv("WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("WEB_PORT", "8095")
    monkeypatch.setenv("WEB_PASSWORD", "env-secret-1")
    cfg = load_config(env_file)
    assert cfg.web_prefix == "/posts" and cfg.hub_url == "/"
    assert cfg.web_password == "env-secret-1"
    assert cfg.web_url == "http://127.0.0.1:8095/posts/"
    assert cfg.version == "2.3.0"
    assert Path(cfg.uploads_dir).is_dir()


def test_load_config_defaults(env):
    _, env_file = env
    cfg = load_config(env_file)
    assert cfg.web_prefix == "" and cfg.hub_url is None and cfg.web_password is None
    assert cfg.web_url == "http://127.0.0.1:8080/"


@pytest.mark.parametrize("key, value, message", [
    ("WEB_PREFIX", "/posts?x", "WEB_PREFIX"),
    ("HUB_URL", "javascript:alert(1)", "HUB_URL"),
    ("WEB_PORT", "eighty", "WEB_PORT"),
    ("WEB_PORT", "70000", "WEB_PORT"),
    ("TIMEZONE", "Mars/Olympus", "TIMEZONE"),
])
def test_load_config_errors_are_value_errors(env, key, value, message):
    monkeypatch, env_file = env
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=message):  # main() prints «Ошибка конфигурации: …»
        load_config(env_file)
