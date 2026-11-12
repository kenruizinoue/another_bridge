"""The launchd service script (scripts/bridge_service.py).

It turns the bridge and its ngrok tunnel into services macOS starts at
login and restarts when they stop. A fake machine stands in for
launchctl, lsof, ps and kill; nothing touches the real launchd or the
running bridge.
"""

from __future__ import annotations

import importlib.util
import plistlib
import signal
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bridge_service.py"
spec = importlib.util.spec_from_file_location("bridge_service", SCRIPT)
svc = importlib.util.module_from_spec(spec)
sys.modules["bridge_service"] = svc  # dataclasses look the module up
spec.loader.exec_module(svc)

URL = "https://fixed-name.ngrok-free.dev"
SERVER_LABEL = svc.LABELS[svc.SERVER]
NGROK_LABEL = svc.LABELS[svc.NGROK]


class FakeMachine:
    def __init__(self) -> None:
        self.loaded: dict = {}  # label -> pid of the running job (or None)
        self.port_pid = None
        self.procs: list = []  # (pid, ppid, command)
        self.calls: list = []
        self.signals: list = []
        self.bootstrap_rc = 0
        self.next_pid = 900

    def run(self, args):
        a = list(args)
        self.calls.append(a)

        def done(rc=0, out="", err=""):
            return subprocess.CompletedProcess(a, rc, out, err)

        if a[:2] == ["launchctl", "print"]:
            label = a[2].split("/", 2)[2]
            if label not in self.loaded:
                return done(113, err="Could not find service")
            pid = self.loaded[label]
            body = "\tstate = running\n" if pid else "\tstate = not running\n"
            body += f"\tpid = {pid}\n" if pid else ""
            return done(out=body + "\tlast exit code = 1\n")
        if a[:2] == ["launchctl", "bootout"]:
            self.loaded.pop(a[2].split("/", 2)[2], None)
            return done()
        if a[:2] == ["launchctl", "bootstrap"]:
            if self.bootstrap_rc:
                return done(self.bootstrap_rc, err="Bootstrap failed: 5: Input/output error")
            with open(a[3], "rb") as f:
                label = plistlib.load(f)["Label"]
            self.next_pid += 1
            self.loaded[label] = self.next_pid
            return done()
        if a[:3] == ["launchctl", "kickstart", "-k"]:
            return done()
        if a[0] == "lsof":
            return done(out=f"{self.port_pid}\n" if self.port_pid else "")
        if a[0] == "ps":
            return done(out="".join(f"{p:>6} {pp:>6} {c}\n" for p, pp, c in self.procs))
        raise AssertionError(f"unexpected command {a}")

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        alive = any(p == pid for p, _, _ in self.procs)
        if not alive:
            raise ProcessLookupError(pid)
        if sig in (signal.SIGTERM, signal.SIGKILL):
            self.procs = [row for row in self.procs if row[0] != pid]
            if self.port_pid == pid:
                self.port_pid = None

    def launchctl(self, verb):
        return [c for c in self.calls if c[:2] == ["launchctl", verb]]


@pytest.fixture
def machine() -> FakeMachine:
    return FakeMachine()


@pytest.fixture
def lines() -> list:
    return []


@pytest.fixture
def env(tmp_path: Path, machine: FakeMachine, lines: list):
    repo = tmp_path / "my repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "uvicorn").write_text("")
    (repo / ".env").write_text(f"# bridge\nWORKSPACE_ROOT=/x\nANOTHER_BRIDGE_NGROK_URL={URL}\n")
    return svc.Env(
        run=machine.run,
        kill=machine.kill,
        sleep=lambda _s: None,
        which=lambda _n: "/opt/homebrew/bin/ngrok",
        health=lambda: True,
        out=lines.append,
        uid=501,
        home=tmp_path / "home",
        repo=repo,
    )


def _plist(env, name) -> dict:
    with env.plist_path(name).open("rb") as f:
        return plistlib.load(f)


# ------------------------------------------------------------------ pieces


def test_read_env_value(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text('# ANOTHER_BRIDGE_NGROK_URL=https://old\nA="quoted"\nB=\nC = spaced \n')
    assert svc.read_env_value(f, "A") == "quoted"
    assert svc.read_env_value(f, "B") is None
    assert svc.read_env_value(f, "C") == "spaced"
    assert svc.read_env_value(f, "ANOTHER_BRIDGE_NGROK_URL") is None
    assert svc.read_env_value(tmp_path / "missing", "A") is None


def test_server_runs_uvicorn_on_localhost_through_the_login_shell(tmp_path: Path) -> None:
    repo = tmp_path / "my repo"
    p = svc.server_plist(repo, tmp_path / "bridge.log")
    assert p["Label"] == SERVER_LABEL
    shell, flags, command = p["ProgramArguments"]
    assert (shell, flags) == ("/bin/zsh", "-lic")
    assert command == f"cd '{repo}' && exec .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000"
    assert "0.0.0.0" not in command
    assert p["RunAtLoad"] is True and p["KeepAlive"] is True
    assert p["StandardOutPath"] == p["StandardErrorPath"] == str(tmp_path / "bridge.log")


def test_ngrok_plist_points_the_fixed_url_at_the_bridge(tmp_path: Path) -> None:
    p = svc.ngrok_plist("/opt/homebrew/bin/ngrok", URL, tmp_path / "ngrok.log", tmp_path)
    assert p["Label"] == NGROK_LABEL
    assert p["ProgramArguments"][:4] == ["/opt/homebrew/bin/ngrok", "http", f"--url={URL}", "8000"]
    assert p["KeepAlive"] is True


# ---------------------------------------------------------------------- on


def test_on_installs_and_starts_both(env, machine, lines) -> None:
    assert svc.main(["on"], env) == 0
    assert _plist(env, svc.SERVER)["Label"] == SERVER_LABEL
    assert f"--url={URL}" in _plist(env, svc.NGROK)["ProgramArguments"]
    assert [c[2:] for c in machine.launchctl("bootstrap")] == [
        ["gui/501", str(env.plist_path(svc.SERVER))],
        ["gui/501", str(env.plist_path(svc.NGROK))],
    ]
    assert set(machine.loaded) == {SERVER_LABEL, NGROK_LABEL}
    assert env.log_dir.is_dir()
    assert any(line.startswith("server: on") for line in lines)


@pytest.mark.parametrize("env_text", ["WORKSPACE_ROOT=/x\n", "ANOTHER_BRIDGE_NGROK_URL=http://plain\n"])
def test_on_needs_an_https_ngrok_url(env, machine, lines, env_text) -> None:
    (env.repo / ".env").write_text(env_text)
    assert svc.main(["on"], env) == 1
    assert "ANOTHER_BRIDGE_NGROK_URL" in lines[0]
    assert not machine.launchctl("bootstrap")
    assert not env.plist_path(svc.SERVER).exists()


def test_on_server_alone_needs_no_url(env, machine) -> None:
    (env.repo / ".env").write_text("WORKSPACE_ROOT=/x\n")
    assert svc.main(["on", "server"], env) == 0
    assert set(machine.loaded) == {SERVER_LABEL}


def test_on_without_ngrok_installed(env, lines) -> None:
    env.which = lambda _n: None
    assert svc.main(["on"], env) == 1
    assert lines == ["ngrok is not on PATH."]


def test_on_without_a_venv(env, lines) -> None:
    (env.repo / ".venv" / "bin" / "uvicorn").unlink()
    assert svc.main(["on", "server"], env) == 1
    assert "No .venv" in lines[0]


def test_on_stops_a_bridge_started_by_hand(env, machine) -> None:
    machine.port_pid = 100
    machine.procs = [(100, 1, "/opt/homebrew/Python .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000")]
    assert svc.main(["on", "server"], env) == 0
    assert machine.signals[0] == (100, signal.SIGTERM)
    assert machine.port_pid is None
    assert SERVER_LABEL in machine.loaded


def test_on_waits_for_a_phone_turn(env, machine, lines) -> None:
    machine.port_pid = 100
    machine.procs = [
        (100, 1, ".venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000"),
        (200, 100, "node /Users/k/.nvm/versions/node/v22/bin/claude -p --resume abc"),
        (201, 100, "/bin/zsh -c helper"),
    ]
    assert svc.main(["on", "server"], env) == 1
    assert "pid 200)" in lines[0]
    assert machine.signals == []
    assert not machine.launchctl("bootstrap")

    assert svc.main(["on", "server", "--force"], env) == 0
    assert (100, signal.SIGTERM) in machine.signals


def test_on_leaves_another_program_on_the_port_alone(env, machine, lines) -> None:
    machine.port_pid = 300
    machine.procs = [(300, 1, "node some-other-server.js")]
    assert svc.main(["on", "server"], env) == 1
    assert "held by another program" in lines[0]
    assert machine.signals == []


def test_on_stops_only_the_ngrok_for_this_url(env, machine) -> None:
    machine.procs = [
        (400, 1, f"ngrok http --url={URL} 8000 --log=stdout"),
        (401, 1, "ngrok http --url=https://other.ngrok-free.dev 3000"),
    ]
    assert svc.main(["on", "ngrok"], env) == 0
    assert [pid for pid, sig in machine.signals if sig == signal.SIGTERM] == [400]
    assert NGROK_LABEL in machine.loaded


def test_on_again_reloads_the_service(env, machine) -> None:
    assert svc.main(["on"], env) == 0
    first = dict(machine.loaded)
    machine.port_pid = first[SERVER_LABEL]
    machine.procs = [(first[SERVER_LABEL], 1, "/bin/zsh -lic cd x && exec .venv/bin/uvicorn main:app")]
    assert svc.main(["on"], env) == 0
    assert [c[2] for c in machine.launchctl("bootout")] == [
        f"gui/501/{SERVER_LABEL}",
        f"gui/501/{NGROK_LABEL}",
    ]
    assert machine.loaded[SERVER_LABEL] != first[SERVER_LABEL]


def test_on_reports_a_launchctl_failure(env, machine, lines) -> None:
    machine.bootstrap_rc = 5
    assert svc.main(["on", "server"], env) == 1
    assert "Input/output error" in lines[-1]


# --------------------------------------------------------------------- off


def test_off_stops_both_and_removes_them_from_login(env, machine, lines) -> None:
    svc.main(["on"], env)
    lines.clear()
    assert svc.main(["off"], env) == 0
    assert machine.loaded == {}
    assert not env.plist_path(svc.SERVER).exists()
    assert not env.plist_path(svc.NGROK).exists()
    assert lines[0].startswith("server: off")


def test_off_ngrok_keeps_the_server(env, machine) -> None:
    svc.main(["on"], env)
    assert svc.main(["off", "ngrok"], env) == 0
    assert set(machine.loaded) == {SERVER_LABEL}
    assert env.plist_path(svc.SERVER).exists()
    assert not env.plist_path(svc.NGROK).exists()


def test_off_when_already_off(env, machine) -> None:
    assert svc.main(["off"], env) == 0
    assert not machine.launchctl("bootout")


def test_off_waits_for_a_phone_turn(env, machine, lines) -> None:
    svc.main(["on", "server"], env)
    pid = machine.loaded[SERVER_LABEL]
    machine.procs = [(pid, 1, "uvicorn"), (500, pid, "claude -p --resume abc")]
    assert svc.main(["off"], env) == 1
    assert SERVER_LABEL in machine.loaded
    assert svc.main(["off", "--force"], env) == 0
    assert machine.loaded == {}


def test_bootout_that_never_finishes(env, machine, monkeypatch) -> None:
    svc.main(["on", "server"], env)
    monkeypatch.setattr(machine, "run", lambda a: subprocess.CompletedProcess(a, 0, "\tpid = 1\n", ""))
    env.run = machine.run
    assert svc.main(["off", "server"], env) == 1


# ----------------------------------------------------------------- restart


def test_restart_kickstarts_the_running_service(env, machine, lines) -> None:
    svc.main(["on"], env)
    assert svc.main(["restart", "server"], env) == 0
    assert ["launchctl", "kickstart", "-k", f"gui/501/{SERVER_LABEL}"] in machine.calls
    assert lines[-1] == "server: restarted."


def test_restart_when_off(env, lines) -> None:
    assert svc.main(["restart", "ngrok"], env) == 1
    assert "ngrok is off" in lines[0]


def test_restart_waits_for_a_phone_turn(env, machine) -> None:
    svc.main(["on", "server"], env)
    pid = machine.loaded[SERVER_LABEL]
    machine.procs = [(500, pid, "claude -p")]
    assert svc.main(["restart", "server"], env) == 1
    assert not any(c[:2] == ["launchctl", "kickstart"] for c in machine.calls)


# ------------------------------------------------------------------ status


def test_status(env, machine, lines) -> None:
    svc.main(["on", "server"], env)
    lines.clear()
    assert svc.main(["status"], env) == 0
    assert lines[0] == f"server: on, pid {machine.loaded[SERVER_LABEL]}"
    assert lines[1] == "ngrok: off"
    assert lines[2].startswith("health: ok")
    assert lines[3] == f"url: {URL}"


def test_status_of_a_service_that_keeps_failing(env, machine, lines) -> None:
    machine.loaded[NGROK_LABEL] = None
    env.health = lambda: False
    svc.main(["status"], env)
    assert lines[1].startswith("ngrok: on, not running (last exit 1)")
    assert lines[2].startswith("health: no answer")


def test_unknown_command_is_rejected(env) -> None:
    with pytest.raises(SystemExit):
        svc.main(["start"], env)
