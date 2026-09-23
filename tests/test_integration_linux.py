import json
import os
import stat
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX shell scripts")

SYSTEMCTL = """#!/bin/sh
case "$1" in
  show) cat "$FAKE_DIR/show.out" ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
"""

JOURNALCTL = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    --after-cursor=*) cat "$FAKE_DIR/journal_new.out"; exit 0 ;;
  esac
done
cat "$FAKE_DIR/journal_first.out"
"""


def journal_json(*messages, start=1):
    return "".join(
        json.dumps({"MESSAGE": m, "__CURSOR": f"s=abc;i={i}", "PRIORITY": "6"}) + "\n"
        for i, m in enumerate(messages, start)
    )


def show(active, sub, restarts=0, result="success"):
    return f"LoadState=loaded\nActiveState={active}\nSubState={sub}\nResult={result}\nNRestarts={restarts}\n"


@pytest.fixture
def rig(tmp_path, telegram):
    bindir, fake = tmp_path / "bin", tmp_path / "fake"
    bindir.mkdir()
    fake.mkdir()
    for name, body in (("systemctl", SYSTEMCTL), ("journalctl", JOURNALCTL)):
        script = bindir / name
        script.write_text(body)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (fake / "show.out").write_text(show("active", "running"))
    (fake / "journal_first.out").write_text(journal_json("Started api"))
    (fake / "journal_new.out").write_text("")
    cfg = tmp_path / "svcwatch.toml"
    cfg.write_text(
        '[monitor]\nstate_file = "state.json"\nsummary_interval = 0\n'
        '[systemd]\ndiscover = "off"\nunits = ["api"]\n'
        f'[notify.telegram]\nbot_token = "{telegram.TOKEN}"\nchat_id = "42"\napi_base = "{telegram.base}"\n'
    )
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(fake)}

    def run_once():
        return subprocess.run(
            [sys.executable, "-m", "svcwatch", "run", "--once", "-c", str(cfg)],
            env=env, capture_output=True, text=True, timeout=60,
        )

    return run_once, fake, telegram


def test_full_lifecycle_through_real_processes(rig):
    run_once, fake, telegram = rig

    first = run_once()
    assert first.returncode == 0, first.stderr
    assert "0 event(s)" in first.stdout and telegram.sent == []

    (fake / "show.out").write_text(show("failed", "failed", result="exit-code"))
    (fake / "journal_new.out").write_text(journal_json(
        "Traceback (most recent call last):", '  File "app.py", line 3, in main', "ValueError: boom", start=2))
    second = run_once()
    assert second.returncode == 0, second.stderr
    texts = [m["text"] for m in telegram.sent]
    assert any("Service down: api" in t and "exit-code" in t for t in texts), texts
    assert any("Error in api" in t and "ValueError: boom" in t for t in texts), texts

    (fake / "journal_new.out").write_text("")
    (fake / "show.out").write_text(show("active", "running"))
    third = run_once()
    assert third.returncode == 0, third.stderr
    assert any("api recovered" in m["text"] for m in telegram.sent)


def test_restart_loop_is_reported(rig):
    run_once, fake, telegram = rig
    run_once()
    (fake / "show.out").write_text(show("active", "running", restarts=3))
    assert run_once().returncode == 0
    assert any("restarted 3 times" in m["text"] for m in telegram.sent)


def test_missing_systemctl_produces_a_readable_alert_not_a_crash(rig, tmp_path):
    run_once, fake, telegram = rig
    (tmp_path / "bin" / "systemctl").unlink()
    proc = run_once()
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
