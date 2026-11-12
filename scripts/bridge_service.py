#!/usr/bin/env python3
"""Run the bridge and its ngrok tunnel as macOS launchd services.

macOS starts both when Ken logs in and starts either again if it stops,
so after a Mac restart the phone works as soon as he logs in. Run it with
the system python from the repo root:

    python3 -I scripts/bridge_service.py on              # install and start both
    python3 -I scripts/bridge_service.py off             # stop both, no start at login
    python3 -I scripts/bridge_service.py off ngrok       # only the tunnel (ticket 31)
    python3 -I scripts/bridge_service.py restart server  # load new code
    python3 -I scripts/bridge_service.py status

The server starts through Ken's login shell (``zsh -lic``) so phone turns
get the same PATH and variables as a terminal tab (``claude`` lives under
nvm). The ngrok URL comes from ``ANOTHER_BRIDGE_NGROK_URL`` in ``.env``.

``on``, ``off`` and ``restart`` refuse while a phone turn runs on the
bridge, because stopping the server cuts that turn's reply. ``--force``
goes ahead anyway.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

SERVER = "server"
NGROK = "ngrok"
NAMES = (SERVER, NGROK)
LABELS = {
    SERVER: "com.kenruizinoue.another-bridge.server",
    NGROK: "com.kenruizinoue.another-bridge.ngrok",
}
LOG_FILES = {SERVER: "bridge.log", NGROK: "ngrok.log"}
PORT = 8000
URL_KEY = "ANOTHER_BRIDGE_NGROK_URL"
SCRIPT = "python3 -I scripts/bridge_service.py"


class ServiceError(Exception):
    pass


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True)


def _health() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3) as r:
            return json.load(r).get("ok") is True
    except Exception:
        return False


@dataclass
class Env:
    """Everything that touches the machine, so tests can swap it out."""

    run: Callable[[Sequence[str]], subprocess.CompletedProcess] = _run
    kill: Callable[[int, int], None] = os.kill
    sleep: Callable[[float], None] = time.sleep
    which: Callable[[str], Optional[str]] = shutil.which
    health: Callable[[], bool] = _health
    out: Callable[[str], None] = print
    uid: int = field(default_factory=os.getuid)
    home: Path = field(default_factory=Path.home)
    repo: Path = Path(__file__).resolve().parent.parent

    @property
    def agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    @property
    def log_dir(self) -> Path:
        return self.home / ".another_coder"

    def plist_path(self, name: str) -> Path:
        return self.agents_dir / f"{LABELS[name]}.plist"

    def target(self, name: str) -> str:
        return f"gui/{self.uid}/{LABELS[name]}"


# ---------------------------------------------------------------- plists


def read_env_value(path: Path, key: str) -> Optional[str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'") or None
    return None


def server_plist(repo: Path, log: Path) -> dict:
    command = (
        f"cd {shlex.quote(str(repo))} && "
        f"exec .venv/bin/uvicorn main:app --host 127.0.0.1 --port {PORT}"
    )
    return {
        "Label": LABELS[SERVER],
        "ProgramArguments": ["/bin/zsh", "-lic", command],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def ngrok_plist(ngrok_bin: str, url: str, log: Path, home: Path) -> dict:
    return {
        "Label": LABELS[NGROK],
        "ProgramArguments": [
            ngrok_bin, "http", f"--url={url}", str(PORT), "--log=stdout", "--log-level=info",
        ],
        "WorkingDirectory": str(home),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


# ------------------------------------------------------------- the machine


def job_info(env: Env, name: str) -> Optional[dict]:
    """launchd's view of a service, or None when it isn't loaded."""
    r = env.run(["launchctl", "print", env.target(name)])
    if r.returncode != 0:
        return None
    info: dict = {"pid": None, "last_exit": None}
    for raw in r.stdout.splitlines():
        line = raw.strip()
        if line.startswith("pid = ") and info["pid"] is None:
            try:
                info["pid"] = int(line[len("pid = "):])
            except ValueError:
                pass
        elif line.startswith("last exit code = ") and info["last_exit"] is None:
            info["last_exit"] = line[len("last exit code = "):]
    return info


def port_holder(env: Env) -> Optional[int]:
    r = env.run(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN", "-t"])
    pids = [int(p) for p in r.stdout.split() if p.isdigit()]
    return pids[0] if pids else None


def processes(env: Env) -> List[tuple]:
    """(pid, ppid, command) for every process."""
    r = env.run(["ps", "-axo", "pid=,ppid=,command="])
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def phone_turns(env: Env, bridge_pid: Optional[int]) -> List[int]:
    if not bridge_pid:
        return []
    return [pid for pid, ppid, cmd in processes(env) if ppid == bridge_pid and "claude" in cmd]


def stop_pid(env: Env, pid: int, port_bound: bool) -> None:
    """SIGTERM, wait up to 15 s (for the port to free, or the pid to go), then SIGKILL."""

    def gone() -> bool:
        if port_bound:
            return port_holder(env) != pid
        try:
            env.kill(pid, 0)
        except OSError:
            return True
        return False

    try:
        env.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(30):
        if gone():
            return
        env.sleep(0.5)
    try:
        env.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def bootout(env: Env, name: str) -> None:
    env.run(["launchctl", "bootout", env.target(name)])
    for _ in range(20):
        if job_info(env, name) is None:
            return
        env.sleep(0.5)
    raise ServiceError(f"launchd did not unload {LABELS[name]}")


def install(env: Env, name: str, plist: dict) -> None:
    if job_info(env, name) is not None:
        bootout(env, name)
    env.agents_dir.mkdir(parents=True, exist_ok=True)
    with env.plist_path(name).open("wb") as f:
        plistlib.dump(plist, f)
    r = env.run(["launchctl", "bootstrap", f"gui/{env.uid}", str(env.plist_path(name))])
    if r.returncode != 0:
        raise ServiceError(f"launchctl bootstrap {LABELS[name]} failed: {r.stderr.strip()}")


def turns_block(env: Env, bridge_pid: Optional[int], force: bool) -> bool:
    """True (and says why) when a phone turn would be cut and --force is off."""
    turns = phone_turns(env, bridge_pid)
    if turns and not force:
        env.out(
            f"A phone turn is running on the bridge (pid {', '.join(map(str, turns))}). "
            "Stopping the server now cuts its reply. Run again when it ends, or add --force."
        )
        return True
    return False


# --------------------------------------------------------------- commands


def cmd_on(env: Env, names: Sequence[str], force: bool) -> int:
    url = ngrok_bin = None
    if NGROK in names:
        url = read_env_value(env.repo / ".env", URL_KEY)
        if not url or not url.startswith("https://"):
            env.out(f"Set {URL_KEY}=https://<your fixed ngrok domain> in {env.repo / '.env'} first.")
            return 1
        ngrok_bin = env.which("ngrok")
        if not ngrok_bin:
            env.out("ngrok is not on PATH.")
            return 1
    if SERVER in names:
        if not (env.repo / ".venv" / "bin" / "uvicorn").exists():
            env.out(f"No .venv in {env.repo}. Create it first (see README, Setup).")
            return 1
        managed = job_info(env, SERVER)
        holder = port_holder(env)
        if holder:
            command = next((c for p, _, c in processes(env) if p == holder), "")
            if (not managed or managed["pid"] != holder) and "uvicorn main:app" not in command:
                env.out(f"Port {PORT} is held by another program (pid {holder}): {command}")
                return 1
            if turns_block(env, holder, force):
                return 1
            if not managed or managed["pid"] != holder:
                env.out(f"Stopping the bridge started by hand (pid {holder}).")
                stop_pid(env, holder, port_bound=True)

    env.log_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name == SERVER:
            plist = server_plist(env.repo, env.log_dir / LOG_FILES[SERVER])
        else:
            managed = job_info(env, NGROK)
            managed_pid = managed["pid"] if managed else None
            for pid, _, command in processes(env):
                if pid != managed_pid and "ngrok" in command and f"--url={url}" in command:
                    env.out(f"Stopping the ngrok started by hand (pid {pid}).")
                    stop_pid(env, pid, port_bound=False)
            plist = ngrok_plist(ngrok_bin, url, env.log_dir / LOG_FILES[NGROK], env.home)
        install(env, name, plist)
        env.out(f"{name}: on. Starts at login and again if it stops. Log: {env.log_dir / LOG_FILES[name]}")
    return 0


def cmd_off(env: Env, names: Sequence[str], force: bool) -> int:
    if SERVER in names:
        managed = job_info(env, SERVER)
        if managed and turns_block(env, managed["pid"], force):
            return 1
    for name in names:
        if job_info(env, name) is not None:
            bootout(env, name)
        env.plist_path(name).unlink(missing_ok=True)
        env.out(f"{name}: off. Stopped, and it won't start at login. Turn on again with: {SCRIPT} on {name}")
    return 0


def cmd_restart(env: Env, names: Sequence[str], force: bool) -> int:
    code = 0
    for name in names:
        info = job_info(env, name)
        if info is None:
            env.out(f"{name} is off. Turn it on with: {SCRIPT} on {name}")
            code = 1
            continue
        if name == SERVER and turns_block(env, info["pid"], force):
            return 1
        r = env.run(["launchctl", "kickstart", "-k", env.target(name)])
        if r.returncode != 0:
            env.out(f"{name}: restart failed: {r.stderr.strip()}")
            code = 1
        else:
            env.out(f"{name}: restarted.")
    return code


def cmd_status(env: Env) -> int:
    for name in NAMES:
        info = job_info(env, name)
        if info is None:
            state = "installed but not loaded" if env.plist_path(name).exists() else "off"
        elif info["pid"]:
            state = f"on, pid {info['pid']}"
        else:
            state = f"on, not running (last exit {info['last_exit']}), launchd retries every 10 s"
        env.out(f"{name}: {state}")
    env.out(f"health: {'ok' if env.health() else 'no answer'} on http://127.0.0.1:{PORT}/health")
    url = read_env_value(env.repo / ".env", URL_KEY)
    if url:
        env.out(f"url: {url}")
    return 0


def main(argv: Optional[Sequence[str]] = None, env: Optional[Env] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the bridge and ngrok as launchd services.")
    parser.add_argument("command", choices=("on", "off", "restart", "status"))
    parser.add_argument("which", nargs="?", choices=NAMES, help="only this one (default: both)")
    parser.add_argument("--force", action="store_true", help="go ahead even if a phone turn runs")
    args = parser.parse_args(argv)
    env = env or Env()
    names = (args.which,) if args.which else NAMES
    try:
        if args.command == "on":
            return cmd_on(env, names, args.force)
        if args.command == "off":
            return cmd_off(env, names, args.force)
        if args.command == "restart":
            return cmd_restart(env, names, args.force)
        return cmd_status(env)
    except ServiceError as err:
        env.out(str(err))
        return 1


if __name__ == "__main__":
    sys.exit(main())
