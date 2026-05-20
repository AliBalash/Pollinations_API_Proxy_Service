from __future__ import annotations

from api.app.config import Settings, load_settings


def test_env_proxy_and_keys(monkeypatch):
    monkeypatch.setenv("POLLINATIONS_API_KEYS", "k1,k2")
    monkeypatch.setenv("POLLINATIONS_USE_PROXY_2080", "true")
    monkeypatch.setenv("POLLINATIONS_PROXY_2080_URL", "socks://127.0.0.1:2080")

    settings: Settings = load_settings(config_file="/tmp/does-not-exist.yml")

    assert settings.pollinations.use_proxy_2080 is True
    assert settings.pollinations.proxy_2080_url == "socks://127.0.0.1:2080"
    assert len(settings.pollinations.api_keys) == 2
