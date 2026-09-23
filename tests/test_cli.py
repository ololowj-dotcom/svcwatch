import socket
import sys
import time
from contextlib import closing

import pytest

from svcwatch import __version__
from svcwatch.cli import build_parser, find_config, main
from svcwatch.config import ConfigError, load_config

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop"


def write(tmp_path, text, name="svcwatch.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("SVCWATCH_CONFIG", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_arguments_prints_help(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    for word in ("setup", "check", "run", "mute", "test-notify", "install-service"):
        assert word in out


def test_every_subcommand_parses():
    parser = build_parser()
    for argv in (["setup", "--yes"], ["init"], ["check"], ["run", "--once"], ["test-notify"],
                 ["telegram-setup", "--token", "x"], ["mute", "2h"], ["unmute"], ["install-service"]):
        assert parser.parse_args(argv).func


def test_init_creates_a_valid_config_and_refuses_to_overwrite(tmp_path, capsys):
    target = tmp_path / "conf" / "svcwatch.toml"
    assert main(["init", str(target)]) == 0
    load_config(target, env={})
    assert main(["init", str(target)]) == 1
    assert "already exists" in capsys.readouterr().out
    assert main(["init", str(target), "--force"]) == 0


def test_missing_config_is_a_friendly_error(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("svcwatch.cli.SEARCH_PATHS", [tmp_path / "nope.toml"])
    assert main(["check"]) == 1
    assert "svcwatch setup" in capsys.readouterr().err
    assert main(["check", "-c", str(tmp_path / "x.toml")]) == 1


def test_invalid_config_lists_the_problems(tmp_path, capsys):
    path = write(tmp_path, "[monitor]\nintervall = 5\n")
    assert main(["check", "-c", path]) == 1
    err = capsys.readouterr().err
    assert "Configuration problem" in err and "did you mean 'interval'" in err


def test_find_config_order(tmp_path, monkeypatch):
    a = write(tmp_path, "", "a.toml")
    assert str(find_config(a)) == a
    monkeypatch.setenv("SVCWATCH_CONFIG", a)
    assert str(find_config(None)) == a
    with pytest.raises(ConfigError):
        find_config(str(tmp_path / "missing.toml"))


def test_check_reports_state_and_uses_exit_codes(tmp_path, capsys):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        path = write(tmp_path, f'[systemd]\nenabled = false\n[[tcp]]\nname = "db"\nport = {port}\nfail_threshold = 1\n')
        assert main(["check", "-c", path]) == 0
        out = capsys.readouterr().out
        assert "OK" in out and "tcp:db" in out and "1 targets" in out
    finally:
        srv.close()
    path = write(tmp_path, f'[systemd]\nenabled = false\n[[tcp]]\nname = "db"\nport = {free_port()}\nfail_threshold = 1\n'
                           'timeout = 1\n', "down.toml")
    assert main(["check", "-c", path]) == 2
    assert "DOWN" in capsys.readouterr().out


def test_check_with_nothing_to_watch(tmp_path, capsys):
    path = write(tmp_path, "[systemd]\nenabled = false\n")
    assert main(["check", "-c", path]) == 1
    assert "Nothing is being watched" in capsys.readouterr().out


def test_check_never_writes_state_or_sends(tmp_path, webhook_sink):
    path = write(tmp_path, f'[systemd]\nenabled = false\n[[tcp]]\nname = "db"\nport = {free_port()}\n'
                           f'fail_threshold = 1\ntimeout = 1\n[notify.webhook]\nurl = "{webhook_sink.url}"\n')
    main(["check", "-c", path])
    assert webhook_sink.received == []
    assert not (tmp_path / "svcwatch-state.json").exists()


def test_run_once_sends_and_saves(tmp_path, webhook_sink, capsys):
    path = write(tmp_path, f'[systemd]\nenabled = false\n[[tcp]]\nname = "db"\nport = {free_port()}\n'
                           f'fail_threshold = 1\ntimeout = 1\n[notify.webhook]\nurl = "{webhook_sink.url}"\n')
    assert main(["run", "--once", "-c", path]) == 0
    assert [p["kind"] for p in webhook_sink.received] == ["down"]
    assert (tmp_path / "svcwatch-state.json").exists()
    assert "1 event(s)" in capsys.readouterr().out


def test_run_once_dry_run_sends_nothing(tmp_path, webhook_sink, capsys):
    path = write(tmp_path, f'[systemd]\nenabled = false\n[[tcp]]\nname = "db"\nport = {free_port()}\n'
                           f'fail_threshold = 1\ntimeout = 1\n[notify.webhook]\nurl = "{webhook_sink.url}"\n')
    assert main(["run", "--once", "--dry-run", "-c", path]) == 0
    assert webhook_sink.received == []
    assert not (tmp_path / "svcwatch-state.json").exists()
    assert "Port down: db" in capsys.readouterr().out


def test_test_notify_ok_and_fail(tmp_path, webhook_sink, capsys):
    path = write(tmp_path, f'[systemd]\nenabled = false\n[notify.webhook]\nurl = "{webhook_sink.url}"\n')
    assert main(["test-notify", "-c", path]) == 0
    out = capsys.readouterr().out
    assert "OK    console" in out and "OK    webhook" in out
    assert webhook_sink.received[0]["title"] == "Test alert from svcwatch"
    dead = write(tmp_path, '[systemd]\nenabled = false\n[notify.webhook]\nurl = "http://127.0.0.1:9/x"\ntimeout = 1\n',
                 "dead.toml")
    assert main(["test-notify", "-c", dead]) == 1
    assert "FAIL  webhook" in capsys.readouterr().out


def test_env_file_option(tmp_path, capsys):
    (tmp_path / "secrets.env").write_text(f"TG={TOKEN}\n")
    path = write(tmp_path, '[systemd]\nenabled = false\n[notify.telegram]\nbot_token = "${TG}"\nchat_id = "1"\n'
                           '[[tcp]]\nname = "x"\nport = 1\n')
    assert main(["check", "-c", path]) == 1
    assert "TG is not set" in capsys.readouterr().err
    assert main(["check", "-c", path, "--env-file", str(tmp_path / "secrets.env")]) in (0, 2)


def test_mute_and_unmute_write_the_shared_mute_file(tmp_path, capsys):
    path = write(tmp_path, "[systemd]\nenabled = false\n")
    assert main(["mute", "30m", "-c", path]) == 0
    mute_file = tmp_path / "svcwatch-state.json.mute"
    assert abs(float(mute_file.read_text()) - (time.time() + 1800)) < 5
    assert main(["mute", "soon", "-c", path]) == 1
    assert main(["unmute", "-c", path]) == 0
    assert not mute_file.exists()
    assert main(["unmute", "-c", path]) == 0


def test_check_shows_active_mute(tmp_path, capsys):
    path = write(tmp_path, f'[systemd]\nenabled = false\n[[tcp]]\nname = "d"\nport = {free_port()}\ntimeout = 1\n')
    main(["mute", "1h", "-c", path])
    capsys.readouterr()
    main(["check", "-c", path])
    assert "muted" in capsys.readouterr().out


def test_install_service_prints_a_hardened_unit(tmp_path, capsys):
    path = write(tmp_path, "")
    assert main(["install-service", "-c", path]) == 0
    out = capsys.readouterr().out
    assert "ExecStart=" in out and "-m svcwatch run --config" in out and "NoNewPrivileges=true" in out
    assert "ProtectSystem=full" in out and "Restart=always" in out
    assert sys.executable.split("\\")[-1] in out or sys.executable in out


def test_telegram_setup_needs_a_token(capsys):
    assert main(["telegram-setup"]) == 1
    assert "bot token" in capsys.readouterr().out.lower()


def test_telegram_setup_finds_the_chat_id(telegram, capsys):
    telegram.message(4242, "/start")
    assert main(["telegram-setup", "--token", TOKEN, "--api-base", telegram.base, "--wait", "3"]) == 0
    out = capsys.readouterr().out
    assert "chat_id = 4242" in out and "watch_test_bot" in out


def test_telegram_setup_bad_token(telegram, capsys):
    assert main(["telegram-setup", "--token", "1:bad", "--api-base", telegram.base]) == 1
    assert "wrong or was revoked" in capsys.readouterr().out


def test_telegram_setup_times_out(telegram, capsys):
    assert main(["telegram-setup", "--token", TOKEN, "--api-base", telegram.base, "--wait", "1"]) == 1
    assert "Nothing received" in capsys.readouterr().out


def test_setup_command_end_to_end(telegram, tmp_path, monkeypatch):
    telegram.message(88, "/start")
    monkeypatch.setattr("svcwatch.wizard.SystemdCfg", lambda: __import__("svcwatch.config", fromlist=["x"]).SystemdCfg(
        unit_dirs=[str(tmp_path / "none")]))
    rc = main(["setup", "--yes", "--no-service", "--dir", str(tmp_path / "out"), "--token", TOKEN,
               "--api-base", telegram.base, "--wait", "3"])
    assert rc == 0
    assert 'chat_id = "88"' in (tmp_path / "out" / "svcwatch.toml").read_text(encoding="utf-8")
