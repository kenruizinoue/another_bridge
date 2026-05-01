"""Coverage for ``services.claude_runner.probe_claude_binary``.

The probe runs once at lifespan startup. Operator UX: a viewer who
just installed via `pip` and runs `uvicorn main:app` will see
``claude.not_invocable`` in the boot log if the CLI isn't on PATH —
much faster diagnosis than waiting for the first chat to surface a
``spawn_failed`` error_kind from the SSE stream.

These tests lock the three failure modes the operator is most
likely to hit (binary missing, non-zero exit, timeout) plus the
happy path. The probe must NEVER raise — the lifespan caller logs
the result and continues; an exception here would prevent the
bridge from booting at all, which is worse than a missing CLI.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from services.claude_runner import probe_claude_binary


class TestProbeClaudeBinary:
    def test_happy_path_returns_version(self) -> None:
        # Simulate ``claude --version`` printing "1.2.3" + exit 0.
        completed = subprocess.CompletedProcess(
            args=["claude", "--version"],
            returncode=0,
            stdout="claude-code 1.2.3\n",
            stderr="",
        )
        with patch("subprocess.run", return_value=completed):
            ok, detail = probe_claude_binary()
        assert ok is True
        assert "1.2.3" in detail

    def test_missing_binary_returns_actionable_hint(self) -> None:
        # FileNotFoundError is what subprocess.run raises when the
        # binary isn't on PATH. The hint should mention CLAUDE_BIN_PATH
        # so the operator knows the fix.
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ok, detail = probe_claude_binary()
        assert ok is False
        assert "CLAUDE_BIN_PATH" in detail

    def test_non_zero_exit_surfaces_stderr(self) -> None:
        # Auth-expired / corrupted-config scenarios usually print the
        # diagnostic to stderr. The probe should surface it verbatim
        # so the operator gets the same message they'd see at the
        # terminal.
        completed = subprocess.CompletedProcess(
            args=["claude", "--version"],
            returncode=1,
            stdout="",
            stderr="auth token expired",
        )
        with patch("subprocess.run", return_value=completed):
            ok, detail = probe_claude_binary()
        assert ok is False
        assert "auth token expired" in detail
        assert "exited with code 1" in detail

    def test_non_zero_with_no_output_falls_back_to_diagnostic(self) -> None:
        # Some `claude` versions exit non-zero without writing to
        # either stream. The probe must still produce a useful message
        # rather than an empty string.
        completed = subprocess.CompletedProcess(
            args=["claude", "--version"],
            returncode=2,
            stdout="",
            stderr="",
        )
        with patch("subprocess.run", return_value=completed):
            ok, detail = probe_claude_binary()
        assert ok is False
        assert "<no output>" in detail

    def test_timeout_returns_clean_message(self) -> None:
        # subprocess.TimeoutExpired fires when claude hangs (e.g.
        # waiting on a network call during auth refresh). The probe
        # must surface "timed out" not just propagate the exception.
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude", "--version"], timeout=5.0),
        ):
            ok, detail = probe_claude_binary(timeout_seconds=5.0)
        assert ok is False
        assert "timed out" in detail

    def test_oserror_at_spawn_returns_clean_message(self) -> None:
        # Permission denied, ENOEXEC, etc. — all the OSError-flavored
        # spawn failures that aren't FileNotFoundError. Should surface
        # the OS message verbatim (operator can google it).
        with patch("subprocess.run", side_effect=OSError("Permission denied")):
            ok, detail = probe_claude_binary()
        assert ok is False
        assert "Permission denied" in detail

    @pytest.mark.parametrize("exc", [
        FileNotFoundError,
        OSError,
        subprocess.TimeoutExpired(cmd=["x"], timeout=1),
    ])
    def test_never_raises(self, exc) -> None:
        # Belt-and-suspenders: the probe is called from lifespan
        # startup. If it raised, the bridge wouldn't boot. Locks the
        # never-raise contract for every error path.
        with patch("subprocess.run", side_effect=exc):
            # No assertion needed — call must complete without
            # raising. The return-value shape is covered by the
            # specific tests above.
            probe_claude_binary()
