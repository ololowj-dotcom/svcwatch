from pathlib import Path

import pytest

from svcwatch.config import load_config
from svcwatch.template import UNIT_TEMPLATE

ROOT = Path(__file__).resolve().parent.parent
ENV = {"TELEGRAM_BOT_TOKEN": "1:abc", "ONCALL_CHAT_ID": "-100200", "TEAM_CHAT_ID": "-100300"}


def test_example_config_is_valid_and_routes_resolve():
    cfg = load_config(ROOT / "examples" / "svcwatch.example.toml", env=ENV)
    assert [t.name for t in cfg.notify.telegram] == ["oncall", "team"]
    assert cfg.notify.email == [] and cfg.notify.webhook == []
    assert cfg.systemd.watch[0].notify == ["oncall"] and cfg.http[0].every == 60
    assert cfg.process[0].min_count == 2


def test_example_needs_its_secrets():
    from svcwatch.config import ConfigError
    with pytest.raises(ConfigError, match="ONCALL_CHAT_ID"):
        load_config(ROOT / "examples" / "svcwatch.example.toml", env={"TELEGRAM_BOT_TOKEN": "1:a"})


def test_static_unit_file_matches_the_generated_one():
    static = (ROOT / "deploy" / "svcwatch.service").read_text(encoding="utf-8")
    generated = UNIT_TEMPLATE.format(exec_start="/opt/svcwatch/venv/bin/python -m svcwatch run --config /etc/svcwatch/svcwatch.toml")
    assert static == generated


def test_installer_is_executable_shell_with_strict_mode():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert "\r\n" not in text
