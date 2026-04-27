"""Tests for the payload _run_planning_job hands to job_manager.mark_done.

The platform's blob dedup relies on `__label__` being present and
formatted as `ticket-<N>-plan` for every plan iteration of the same
ticket. If the format ever drifts (refactor renames it, someone tweaks
the f-string, or the field gets removed entirely) the dedup breaks
silently — the platform sees different labels for what should be the
same logical artifact, both blobs render in next-turn prompts, and
the user re-experiences the original "blob accumulation" bug.

This is the regression test that locks the contract.

Mocks subprocess.Popen + the build-helpers so the planning job runs
synchronously in-test without spawning Claude. We're testing the
payload shape, not the subprocess machinery (covered separately in
test_jobs_cancel.py).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jobs import job_manager
from routers import planning as planning_router


class _StubProcCompleted:
    """Stand-in for subprocess.Popen that already finished cleanly with
    a known stdout. Mirrors the Popen surface _run_planning_job touches:
    args, returncode, communicate (returns stdout/stderr tuple), pid."""

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.args: list[str] = ["claude"]
        self.pid = 99999
        self.returncode = returncode
        self._stdout = stdout

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return (self._stdout, "")

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:  # pragma: no cover — never called on completed proc
        pass


@pytest.fixture
def captured_payload() -> dict[str, Any]:
    """Captures the dict passed to job_manager.mark_done."""
    box: dict[str, Any] = {}

    def _capture(job_id: str, payload: dict[str, Any]) -> None:
        box["job_id"] = job_id
        box["payload"] = payload

    with patch.object(job_manager, "mark_done", side_effect=_capture):
        yield box


@pytest.fixture
def stub_popen():
    """Patches subprocess.Popen in the planning module so the runner
    doesn't try to spawn `claude`. Returns the patcher so individual
    tests can configure stdout per case."""
    with patch.object(planning_router, "subprocess") as mock_sub:
        # Mirror the real submodule shape the runner uses.
        mock_sub.PIPE = -1  # arbitrary sentinel; we never read it
        mock_sub.TimeoutExpired = type("TimeoutExpired", (Exception,), {})
        # Also forward CompletedProcess for any fallback paths.
        import subprocess as real_subprocess

        mock_sub.CompletedProcess = real_subprocess.CompletedProcess
        yield mock_sub


@pytest.fixture(autouse=True)
def _stub_repo_helpers():
    """_run_planning_job calls _build_selected_repo_context which reads
    git origin from disk. Stub it so the test doesn't depend on the
    actual repo state."""
    with patch.object(
        planning_router,
        "_build_selected_repo_context",
        return_value={"path": "/tmp/fake-repo", "name": "fake-repo"},
    ):
        yield


# ──────────────────────────────────────────────────────────────────────
# __label__ format — the dedup contract
# ──────────────────────────────────────────────────────────────────────


class TestLabelFormat:
    def test_payload_includes_label_for_dedup(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        stub_popen.Popen.return_value = _StubProcCompleted(
            stdout="## Plan\n\n1. step one\n\nSUMMARY: short summary"
        )

        planning_router._run_planning_job(
            job_id="job-123",
            ticket_number=42,
            ticket_body="some body",
            resolved_repo_path="/tmp/fake-repo",
        )

        payload = captured_payload["payload"]
        assert "__label__" in payload, (
            "payload must include __label__ — without it the platform "
            "cannot dedup plan iterations"
        )
        assert payload["__label__"] == "ticket-42-plan", (
            "label format MUST stay ticket-<N>-plan — changing it "
            "breaks dedup for previously-stored plans"
        )

    def test_label_uses_provided_ticket_number_verbatim(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        # Defensive: if the f-string ever loses {ticket_number}, every
        # ticket would dedup against everything else. Spot-check a
        # different number than above.
        stub_popen.Popen.return_value = _StubProcCompleted(stdout="...")

        planning_router._run_planning_job(
            job_id="job-456",
            ticket_number=104,
            ticket_body="body",
            resolved_repo_path="/tmp/fake-repo",
        )

        assert captured_payload["payload"]["__label__"] == "ticket-104-plan"

    def test_label_present_even_when_summary_missing(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        # __label__ and __summary__ are independent opt-ins. Plans that
        # don't end with a SUMMARY: line still need the dedup label.
        stub_popen.Popen.return_value = _StubProcCompleted(
            stdout="## Plan\n\n1. step one\n2. step two"
        )

        planning_router._run_planning_job(
            job_id="job-789",
            ticket_number=7,
            ticket_body="body",
            resolved_repo_path="/tmp/fake-repo",
        )

        payload = captured_payload["payload"]
        assert payload["__label__"] == "ticket-7-plan"
        assert "__summary__" not in payload  # confirms independence

    def test_label_present_when_summary_present(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        # Belt-and-suspenders: both fields coexist.
        stub_popen.Popen.return_value = _StubProcCompleted(
            stdout="## Plan\n1. step\nSUMMARY: tight one-liner"
        )

        planning_router._run_planning_job(
            job_id="job-999",
            ticket_number=1,
            ticket_body="body",
            resolved_repo_path="/tmp/fake-repo",
        )

        payload = captured_payload["payload"]
        assert payload["__label__"] == "ticket-1-plan"
        assert payload["__summary__"] == "tight one-liner"


# ──────────────────────────────────────────────────────────────────────
# Other payload fields — sanity, not exhaustive
# ──────────────────────────────────────────────────────────────────────


class TestPayloadShape:
    def test_payload_includes_required_fields(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        # Locks down that the full payload contract — plan + label +
        # context + ticket info — is wired. If any of these gets dropped
        # the agent flow downstream breaks in a non-obvious way.
        stub_popen.Popen.return_value = _StubProcCompleted(stdout="## Plan\n1. step")

        planning_router._run_planning_job(
            job_id="job-x",
            ticket_number=2,
            ticket_body="body",
            resolved_repo_path="/tmp/fake-repo",
        )

        p = captured_payload["payload"]
        assert "plan" in p
        assert p["ticket_number"] == 2
        assert p["repo_path"] == "/tmp/fake-repo"
        assert p["exit_code"] == 0
        assert "duration_seconds" in p
        assert "__context__" in p
        assert "selected_repo" in p["__context__"]
        assert "__label__" in p
