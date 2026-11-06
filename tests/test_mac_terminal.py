"""Opening a session in a Mac terminal tab: the command, the AppleScript
and the app choice (ticket 25). osascript itself is faked."""

from __future__ import annotations

import subprocess

import pytest

from services import mac_terminal
from services.mac_terminal import OpenError

SID = "509b9cde-221d-47c3-b7ee-c3a0c7d0920e"


class TestResumeCommand:
    def test_cds_into_the_folder_and_resumes(self) -> None:
        cmd = mac_terminal.resume_command(SID, "/Users/ken", skip_permissions=True)
        assert cmd == f"cd /Users/ken && claude --dangerously-skip-permissions --resume {SID}"

    def test_without_skip_permissions(self) -> None:
        cmd = mac_terminal.resume_command(SID, "/Users/ken", skip_permissions=False)
        assert cmd == f"cd /Users/ken && claude --resume {SID}"

    def test_quotes_a_folder_with_spaces_and_quotes(self) -> None:
        cmd = mac_terminal.resume_command(SID, "/Users/ken/My \"odd\" folder", skip_permissions=False)
        assert cmd.startswith("cd '/Users/ken/My \"odd\" folder' && ")

    def test_a_folder_cannot_inject_a_command(self) -> None:
        cmd = mac_terminal.resume_command(SID, "/tmp/x; rm -rf ~", skip_permissions=False)
        assert cmd.startswith("cd '/tmp/x; rm -rf ~' && ")

    @pytest.mark.parametrize("bad", ["", "a b", "x;rm", "../etc", "-flag", "a" * 65])
    def test_refuses_anything_that_is_not_an_id(self, bad: str) -> None:
        with pytest.raises(OpenError):
            mac_terminal.resume_command(bad, "/Users/ken", skip_permissions=False)

    @pytest.mark.parametrize("cwd", ["", "/Users/ken\nrm -rf ~", "/Users/\x07ken"])
    def test_refuses_an_empty_folder_or_control_characters(self, cwd: str) -> None:
        with pytest.raises(OpenError):
            mac_terminal.resume_command(SID, cwd, skip_permissions=False)


class TestScripts:
    def test_iterm_opens_a_tab_or_a_window_and_types_the_command(self) -> None:
        script = mac_terminal.iterm_script('cd /x && claude --resume "id"')
        assert 'tell application "iTerm"' in script
        assert "create tab with default profile" in script
        assert "create window with default profile" in script
        assert 'write text "cd /x && claude --resume \\"id\\""' in script

    def test_terminal_runs_the_command_in_a_new_window(self) -> None:
        script = mac_terminal.terminal_script("cd /x && claude")
        assert 'tell application "Terminal"' in script
        assert 'do script "cd /x && claude"' in script

    def test_backslashes_are_escaped_before_quotes(self) -> None:
        script = mac_terminal.terminal_script('a\\"b')
        assert 'do script "a\\\\\\"b"' in script


class TestPickApp:
    def test_auto_prefers_iterm(self) -> None:
        assert mac_terminal.pick_app("auto", iterm_installed=True) == "iterm"

    def test_auto_falls_back_to_terminal(self) -> None:
        assert mac_terminal.pick_app("auto", iterm_installed=False) == "terminal"

    def test_iterm_falls_back_to_terminal_when_missing(self) -> None:
        assert mac_terminal.pick_app("iterm", iterm_installed=False) == "terminal"

    def test_terminal_always_uses_terminal(self) -> None:
        assert mac_terminal.pick_app("terminal", iterm_installed=True) == "terminal"

    def test_unknown_preference_is_refused(self) -> None:
        with pytest.raises(OpenError):
            mac_terminal.pick_app("warp", iterm_installed=True)


class TestOpenTab:
    def test_runs_osascript_with_the_apps_script(self) -> None:
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        mac_terminal.open_tab("cd /x && claude", "terminal", run=fake_run)
        assert calls[0][:2] == ["osascript", "-e"]
        assert 'tell application "Terminal"' in calls[0][2]

    def test_a_failed_osascript_raises_with_its_message(self) -> None:
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "Not authorized to send Apple events to iTerm. (-1743)")

        with pytest.raises(OpenError, match="-1743"):
            mac_terminal.open_tab("cd /x && claude", "iterm", run=fake_run)

    def test_a_hung_osascript_raises(self) -> None:
        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 20)

        with pytest.raises(OpenError):
            mac_terminal.open_tab("cd /x && claude", "iterm", run=fake_run)


class TestValidateConfig:
    def test_accepts_known_apps_and_an_existing_folder(self, tmp_path) -> None:
        for app in ("auto", "iterm", "terminal"):
            mac_terminal.validate_config(app, tmp_path)

    def test_refuses_an_unknown_app(self, tmp_path) -> None:
        with pytest.raises(OpenError, match="TERMINAL_APP"):
            mac_terminal.validate_config("warp", tmp_path)

    def test_refuses_a_missing_folder(self, tmp_path) -> None:
        with pytest.raises(OpenError, match="NEW_SESSION_CWD"):
            mac_terminal.validate_config("auto", tmp_path / "missing")


class TestResumeProcesses:
    PS = (
        f"  101     1 claude --dangerously-skip-permissions --resume {SID}\n"
        f"  102   900 /usr/local/bin/claude -p hi --resume {SID} --model x\n"  # a phone turn
        f"  103     1 claude -r {SID}\n"
        "  104     1 claude --resume other-id\n"
        f"  105     1 vim notes-{SID}.txt\n"
        "garbage line\n"
    )

    def _run(self, stdout: str):
        return lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout, "")

    def test_finds_terminal_copies_but_not_the_bridges_own_turns(self) -> None:
        pids = mac_terminal.resume_processes(SID, exclude_parent=900, run=self._run(self.PS))
        assert pids == [101, 103]

    def test_a_failing_ps_finds_nothing(self) -> None:
        def boom(args, **kw):
            raise OSError("no ps")

        assert mac_terminal.resume_processes(SID, exclude_parent=900, run=boom) == []
