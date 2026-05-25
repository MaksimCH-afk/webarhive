"""Test that UI-supplied overrides land in data/settings.json and that
subsequent get_settings() picks them up (spec §11, §15)."""

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def isolated_overrides(tmp_path, monkeypatch):
    """Run with a private OVERRIDES_PATH so tests don't pollute the repo."""
    import webarhive.config.settings as settings_mod
    fake = tmp_path / "settings.json"
    monkeypatch.setattr(settings_mod, "OVERRIDES_PATH", fake)
    settings_mod.get_settings.cache_clear()
    monkeypatch.chdir(tmp_path)
    yield fake
    settings_mod.get_settings.cache_clear()


def test_overrides_round_trip(isolated_overrides):
    from webarhive.config import get_settings
    from webarhive.config.settings import save_overrides

    before = get_settings()
    default_threshold = before.title_shift_threshold

    save_overrides({"title_shift_threshold": default_threshold + 5,
                    "concurrency": 7,
                    "enable_verdict": False})

    saved = json.loads(isolated_overrides.read_text())
    assert saved["title_shift_threshold"] == default_threshold + 5
    assert saved["concurrency"] == 7
    assert saved["enable_verdict"] is False

    after = get_settings()
    assert after.title_shift_threshold == default_threshold + 5
    assert after.concurrency == 7
    assert after.enable_verdict is False


def test_overrides_whitelist_drops_non_editable(isolated_overrides):
    """API keys (openrouter, whois) are NOW editable from the UI by
    explicit user request. Deployment-level things (database_url,
    app_domain) stay locked to .env."""
    from webarhive.config import get_settings
    from webarhive.config.settings import save_overrides

    save_overrides({
        "openrouter_api_key": "sk-or-v1-from-ui",   # editable now
        "whois_api_key": "wh-from-ui",              # editable now
        "database_url": "evil://",                  # NOT editable
        "app_domain": "evil.example",               # NOT editable
        "concurrency": 9,                           # legit
    })
    saved = json.loads(isolated_overrides.read_text())
    assert saved.get("openrouter_api_key") == "sk-or-v1-from-ui"
    assert saved.get("whois_api_key") == "wh-from-ui"
    assert "database_url" not in saved
    assert "app_domain" not in saved
    assert saved["concurrency"] == 9


def test_snapshot_reflects_overrides(isolated_overrides):
    from webarhive.config import get_settings
    from webarhive.config.settings import save_overrides

    save_overrides({"model_classification": "anthropic/claude-haiku",
                    "enable_smart_drop": True})
    s = get_settings()
    snap = s.snapshot()
    assert snap["models"]["classification"] == "anthropic/claude-haiku"
    assert snap["roles"]["smart_drop"] is True
