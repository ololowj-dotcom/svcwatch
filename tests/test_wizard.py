import stat
import sys

import pytest
from conftest import FakeRunner, journal, show

from svcwatch import wizard
from svcwatch.config import SystemdCfg, load_config
from svcwatch.runner import CmdResult
from svcwatch.wizard import IO, SetupArgs, install_service, run_setup

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop"


class ScriptIO(IO):
    def __init__(self, *answers):
        self.answers = list(answers)
        self.out = []

    def say(self, text=""):
        self.out.append(text)

    def ask(self, prompt, default=None, secret=False):
        self.out.append(f"? {prompt}")
        value = self.answers.pop(0) if self.answers else ""
        return value or default or ""

    def confirm(self, prompt, default=True):
        answer = (self.ask(prompt) or "").lower()
        return default if not answer else answer in ("y", "yes")

    @property
    def text(self):
        return "\n".join(self.out)


@pytest.fixture
def env(tmp_path, monkeypatch, telegram):
    units = tmp_path / "units"
    units.mkdir()
    (units / "api.service").write_text("x")
    (units / "worker.service").write_text("x")
    monkeypatch.setattr(wizard, "SystemdCfg", lambda: SystemdCfg(unit_dirs=[str(units)]))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    runner = FakeRunner()
    runner.on("systemctl show", stdout=show())
    runner.on("journalctl", stdout=journal("anchor"))
    runner.on("docker ps", stdout="")

    def args(**kw):
        base = dict(directory=tmp_path / "cfg", token=TOKEN, yes=True, no_service=True, wait=2,
                    api_base=telegram.base)
        base.update(kw)
        return SetupArgs(**base)

    return runner, args, tmp_path


def test_happy_path_writes_working_config_and_secret_file(env, telegram):
    runner, args, tmp_path = env
    telegram.message(555, "/start")
    io = ScriptIO()
    assert run_setup(args(), io, runner) == 0
    cfg_file, env_file = tmp_path / "cfg" / "svcwatch.toml", tmp_path / "cfg" / ".env"
    text = cfg_file.read_text(encoding="utf-8")
    assert 'chat_id = "555"' in text and "${TELEGRAM_BOT_TOKEN}" in text
    assert TOKEN not in text
    assert f"TELEGRAM_BOT_TOKEN={TOKEN}" in env_file.read_text()
    cfg = load_config(cfg_file, env={})
    assert cfg.notify.telegram[0].bot_token == TOKEN and cfg.notify.telegram[0].commands
    assert cfg.notify.telegram[0].api_base == telegram.base
    assert any("svcwatch connected" in m["text"] for m in telegram.sent)
    assert "watch_test_bot" in io.text and "api, worker" in io.text
    assert "targets checked" in io.text and "Config is valid" in io.text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_env_file_is_private(env, telegram):
    runner, args, tmp_path = env
    telegram.message(1)
    run_setup(args(), ScriptIO(), runner)
    mode = stat.S_IMODE((tmp_path / "cfg" / ".env").stat().st_mode)
    assert mode == 0o600


def test_explicit_chat_id_skips_discovery(env, telegram):
    runner, args, _ = env
    assert run_setup(args(chat_id="-100777"), ScriptIO(), runner) == 0
    assert "getUpdates" not in telegram.requests
    assert telegram.sent[0]["chat_id"] == "-100777"


def test_token_can_come_from_the_environment(env, telegram, monkeypatch):
    runner, args, _ = env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    assert run_setup(args(token=None, chat_id="1"), ScriptIO(), runner) == 0


def test_token_can_be_typed_interactively_and_bad_input_is_retried(env, telegram):
    runner, args, _ = env
    io = ScriptIO("hello", TOKEN)
    assert run_setup(args(token=None, chat_id="1", yes=False), io, runner) == 0
    assert "does not look like a bot token" in io.text


def test_three_bad_tokens_give_up(env):
    runner, args, tmp_path = env
    io = ScriptIO("a", "b", "c")
    assert run_setup(args(token=None, yes=False), io, runner) == 1
    assert not (tmp_path / "cfg").exists()


def test_revoked_token_is_explained(env, telegram):
    runner, args, tmp_path = env
    io = ScriptIO()
    assert run_setup(args(token="999999999:AAWrongTokenWrongTokenWrongTokenXX"), io, runner) == 1
    assert "wrong or was revoked" in io.text and not (tmp_path / "cfg").exists()


def test_bot_with_a_webhook_is_refused(env, telegram):
    runner, args, _ = env
    telegram.webhook = "https://example.com/hook"
    io = ScriptIO()
    assert run_setup(args(), io, runner) == 1
    assert "webhook" in io.text


def test_no_message_within_the_wait_time_fails_cleanly(env, telegram):
    runner, args, tmp_path = env
    io = ScriptIO()
    assert run_setup(args(wait=1), io, runner) == 1
    assert "Nothing received" in io.text and not (tmp_path / "cfg").exists()


def test_several_chats_lets_you_choose(env, telegram):
    runner, args, tmp_path = env
    telegram.message(111, "hi", update_id=1)
    telegram.updates.append({"update_id": 2, "message": {
        "chat": {"id": -222, "type": "group", "title": "Ops room"}, "text": "hello"}})
    io = ScriptIO("2")
    assert run_setup(args(yes=False), io, runner) == 0
    assert "Several chats" in io.text
    assert 'chat_id = "111"' in (tmp_path / "cfg" / "svcwatch.toml").read_text(encoding="utf-8")


def test_message_send_failure_stops_before_writing(env, telegram):
    runner, args, tmp_path = env
    telegram.message(5)
    telegram.fail_send = {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}
    io = ScriptIO()
    assert run_setup(args(), io, runner) == 1
    assert "Could not send" in io.text and not (tmp_path / "cfg").exists()


def test_existing_config_is_protected_unless_forced(env, telegram):
    runner, args, tmp_path = env
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "svcwatch.toml").write_text("# precious\n", encoding="utf-8")
    assert run_setup(args(chat_id="1"), ScriptIO(), runner) == 1
    assert (cfg_dir / "svcwatch.toml").read_text() == "# precious\n"
    assert run_setup(args(chat_id="1", force=True), ScriptIO(), runner) == 0
    assert (cfg_dir / "svcwatch.toml.bak").read_text() == "# precious\n"
    assert "precious" not in (cfg_dir / "svcwatch.toml").read_text()


def test_existing_env_keeps_other_secrets(env, telegram):
    runner, args, tmp_path = env
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / ".env").write_text("SMTP_PASSWORD=keepme\nTELEGRAM_BOT_TOKEN=old\n")
    assert run_setup(args(chat_id="1"), ScriptIO(), runner) == 0
    lines = (cfg_dir / ".env").read_text().splitlines()
    assert "SMTP_PASSWORD=keepme" in lines and f"TELEGRAM_BOT_TOKEN={TOKEN}" in lines
    assert not any(line == "TELEGRAM_BOT_TOKEN=old" for line in lines)


def test_docker_question_is_asked_only_when_containers_exist(env, telegram, monkeypatch):
    runner, args, tmp_path = env
    monkeypatch.setattr(wizard.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    runner.on("docker ps", stdout="web\ndb\n")
    runner.on("docker inspect -f", stdout="running|0|0|none|false\n")
    runner.on("docker logs", stdout="")
    io = ScriptIO("n")
    assert run_setup(args(chat_id="1", yes=False), io, runner) == 0
    assert "Also watch 2 Docker containers" in io.text
    assert "enabled = false" in (tmp_path / "cfg" / "svcwatch.toml").read_text(encoding="utf-8")
    runner.on("docker ps", stdout="web\n")
    assert run_setup(args(chat_id="1", force=True), ScriptIO(), runner) == 0
    assert "[docker]\nenabled = true" in (tmp_path / "cfg" / "svcwatch.toml").read_text(encoding="utf-8")


def test_no_custom_services_is_not_an_error(env, telegram, monkeypatch, tmp_path):
    runner, args, _ = env
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(wizard, "SystemdCfg", lambda: SystemdCfg(unit_dirs=[str(empty)]))
    io = ScriptIO()
    assert run_setup(args(chat_id="1"), io, runner) == 0
    assert "No custom services found" in io.text


def test_install_service_writes_unit_and_enables_it(tmp_path):
    runner = FakeRunner()
    runner.on("systemctl daemon-reload")
    runner.on("systemctl enable --now svcwatch")
    runner.on("systemctl is-active", stdout="active\n")
    unit = tmp_path / "svcwatch.service"
    io = ScriptIO()
    assert install_service(str(tmp_path / "svcwatch.toml"), runner=runner, io=io, unit_path=unit) == 0
    text = unit.read_text()
    assert "ExecStart=" in text and "-m svcwatch run --config" in text and "NoNewPrivileges=true" in text
    assert [c[:2] for c in runner.calls[:2]] == [["systemctl", "daemon-reload"], ["systemctl", "enable"]]
    assert "service is active" in io.text


def test_install_service_reports_systemctl_failure(tmp_path):
    runner = FakeRunner()
    runner.on("systemctl daemon-reload", stderr="Access denied", returncode=1)
    io = ScriptIO()
    assert install_service("c.toml", runner=runner, io=io, unit_path=tmp_path / "u.service") == 1
    assert "Access denied" in io.text


def test_install_service_without_start(tmp_path):
    runner = FakeRunner()
    runner.on("systemctl daemon-reload")
    assert install_service("c.toml", runner=runner, start=False, io=ScriptIO(), unit_path=tmp_path / "u") == 0
    assert not runner.called("systemctl enable")


def test_service_offer_when_not_root_only_prints_instructions(env, telegram, monkeypatch):
    runner, args, _ = env
    monkeypatch.setattr(wizard, "is_root", lambda: False)
    monkeypatch.setattr(wizard.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None)
    io = ScriptIO()
    assert run_setup(args(chat_id="1", no_service=False), io, runner) == 0
    assert "re-run as root" in io.text and not runner.called("systemctl enable")


def test_service_offer_as_root_installs_it(env, telegram, monkeypatch, tmp_path):
    runner, args, _ = env
    monkeypatch.setattr(wizard, "is_root", lambda: True)
    monkeypatch.setattr(wizard.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(wizard, "UNIT_PATH", tmp_path / "svcwatch.service")
    monkeypatch.setattr(wizard, "install_service",
                        lambda path, **kw: (setattr(wizard, "_installed", path), 0)[1])
    io = ScriptIO()
    assert run_setup(args(chat_id="1", no_service=False), io, runner) == 0
    assert wizard._installed.endswith("svcwatch.toml")


_ = CmdResult
