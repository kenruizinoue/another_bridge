"""Tests for the payload _run_planning_job hands to job_manager.mark_done.

The platform's blob dedup relies on `__label__` being present and
formatted as `plan-<repo_short_name>-<N>` for every plan iteration of
the same (repo, ticket) pair. If the format ever drifts (refactor
renames it, someone tweaks the f-string, drops the repo segment, or
the field gets removed entirely) the dedup breaks silently — the
platform either sees different labels for what should be the same
logical artifact (blob accumulation returns), or worse, sees the same
label for plans of different tickets/repos (cross-repo collision; one
plan supersedes another it has nothing to do with).

This is the regression test that locks the contract.

Mocks claude_runner.run_blocking + the build-helpers so the planning
job runs synchronously in-test without spawning Claude. We're testing
the payload shape, not the subprocess machinery (covered separately
in test_jobs_cancel.py).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from jobs import job_manager
from routers import planning as planning_router
from services import claude_runner


def _make_result(stdout: str, returncode: int = 0) -> claude_runner.ClaudeResult:
    """Build a ClaudeResult that looks like a clean (or non-zero-exit)
    blocking run. Tests configure stdout to drive the parser path."""
    return claude_runner.ClaudeResult(
        returncode=returncode,
        stdout=stdout,
        stderr="",
        timed_out=False,
        cancelled=False,
        duration_seconds=0.5,
    )


@pytest.fixture
def captured_payload() -> dict[str, Any]:
    """Captures the dict passed to job_manager.mark_done."""
    box: dict[str, Any] = {}

    def _capture(job_id: str, payload: dict[str, Any]) -> None:
        box["job_id"] = job_id
        box["payload"] = payload

    with patch.object(job_manager, "mark_done", side_effect=_capture):
        yield box


class _StubRunBlocking:
    """Configurable stand-in for claude_runner.run_blocking. Tests set
    `.return_value` to a ClaudeResult and the planner picks it up. Mirrors
    the original Popen-mock ergonomics so each test can stage a different
    stdout per call."""

    def __init__(self) -> None:
        self.return_value: claude_runner.ClaudeResult = _make_result("")

    def __call__(self, *args: Any, **kwargs: Any) -> claude_runner.ClaudeResult:
        return self.return_value


@pytest.fixture
def stub_popen():
    """Patches claude_runner.run_blocking so the planning runner doesn't
    spawn `claude`. Name kept as `stub_popen` so existing test bodies
    that set `stub_popen.Popen.return_value = _StubProcCompleted(...)`
    keep reading. The shim translates the legacy assignment into the
    new ClaudeResult shape transparently."""

    class _LegacyShim:
        """Bridges the old `stub_popen.Popen.return_value = _StubProcCompleted(...)`
        assignment style to the new run_blocking-patch model. Each test still
        reads natural — no rewrites of test bodies — and the shim builds a
        ClaudeResult from the stub proc's stdout/returncode."""

        def __init__(self, run_stub: _StubRunBlocking) -> None:
            self._run = run_stub
            self.Popen = self  # so `stub_popen.Popen.return_value = X` works

        @property
        def return_value(self) -> Any:
            return None

        @return_value.setter
        def return_value(self, stub_proc: Any) -> None:
            self._run.return_value = _make_result(
                stdout=getattr(stub_proc, "_stdout", ""),
                returncode=getattr(stub_proc, "returncode", 0),
            )

    run_stub = _StubRunBlocking()
    with patch.object(claude_runner, "run_blocking", run_stub):
        yield _LegacyShim(run_stub)


class _StubProcCompleted:
    """Stand-in matching the legacy Popen-stub shape used in test bodies.
    Only the stdout + returncode fields are read by the shim — the rest
    exist for any test that introspects them."""

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.args: list[str] = ["claude"]
        self.pid = 99999
        self.returncode = returncode
        self._stdout = stdout

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return (self._stdout, "")

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:  # pragma: no cover
        pass


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
        assert payload["__label__"] == "plan-fake-repo-42", (
            "label format MUST stay plan-<repo>-<N> — changing it "
            "breaks dedup for previously-stored plans and risks "
            "cross-repo collisions when ticket numbers overlap"
        )

    def test_label_uses_provided_ticket_number_verbatim(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        # Defensive: if the f-string ever loses {ticket_number}, every
        # ticket in the same repo would dedup against everything else.
        stub_popen.Popen.return_value = _StubProcCompleted(stdout="...")

        planning_router._run_planning_job(
            job_id="job-456",
            ticket_number=104,
            ticket_body="body",
            resolved_repo_path="/tmp/fake-repo",
        )

        assert captured_payload["payload"]["__label__"] == "plan-fake-repo-104"

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
        assert payload["__label__"] == "plan-fake-repo-7"
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
        assert payload["__label__"] == "plan-fake-repo-1"
        assert payload["__summary__"] == "tight one-liner"


# ──────────────────────────────────────────────────────────────────────
# Repo isolation — the bug this format change fixed
# ──────────────────────────────────────────────────────────────────────


class TestRepoIsolation:
    """Earlier label format `ticket-<N>-plan` collided across repos:
    plan ticket #2 in `another_coder` then plan ticket #2 in
    `another_agent_backend` and the second plan would supersede the
    first in the next-turn render — even though they're plans for
    completely different work. The new format `plan-<repo>-<N>` keeps
    them isolated. These tests lock that down."""

    def test_label_uses_repo_short_name_from_path(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        # Long absolute path with workspace prefix, exactly like
        # production. The label should pick up just the basename.
        stub_popen.Popen.return_value = _StubProcCompleted(stdout="...")

        planning_router._run_planning_job(
            job_id="j",
            ticket_number=2,
            ticket_body="body",
            resolved_repo_path="/Users/x/Desktop/AnohterAgent Projects/another_coder",
        )

        assert captured_payload["payload"]["__label__"] == "plan-another_coder-2"

    def test_same_ticket_different_repos_get_different_labels(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        # The actual regression: ticket #2 in repo A and ticket #2 in
        # repo B must NOT collide. Run the job twice with the same
        # ticket number but different repo paths and confirm the
        # labels diverge.
        stub_popen.Popen.return_value = _StubProcCompleted(stdout="...")

        planning_router._run_planning_job(
            job_id="job-A",
            ticket_number=2,
            ticket_body="body",
            resolved_repo_path="/workspace/another_coder",
        )
        label_a = captured_payload["payload"]["__label__"]

        # Reset stub_popen so the second call also has a value
        stub_popen.Popen.return_value = _StubProcCompleted(stdout="...")

        planning_router._run_planning_job(
            job_id="job-B",
            ticket_number=2,
            ticket_body="body",
            resolved_repo_path="/workspace/another_agent_backend",
        )
        label_b = captured_payload["payload"]["__label__"]

        assert label_a == "plan-another_coder-2"
        assert label_b == "plan-another_agent_backend-2"
        assert label_a != label_b, (
            "same ticket number in different repos MUST produce "
            "different labels — otherwise one plan supersedes another "
            "unrelated one in the conversation artifacts render"
        )

    def test_label_handles_trailing_slash_in_repo_path(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        # Defensive: os.path.basename of `/foo/bar/` is "" without
        # rstrip. The f-string uses .rstrip('/') so both forms produce
        # the same label. If the strip ever gets dropped, dedup keys
        # would diverge between callers passing trailing-slash and not.
        stub_popen.Popen.return_value = _StubProcCompleted(stdout="...")

        planning_router._run_planning_job(
            job_id="j1",
            ticket_number=5,
            ticket_body="body",
            resolved_repo_path="/workspace/myrepo/",
        )
        with_slash = captured_payload["payload"]["__label__"]

        stub_popen.Popen.return_value = _StubProcCompleted(stdout="...")
        planning_router._run_planning_job(
            job_id="j2",
            ticket_number=5,
            ticket_body="body",
            resolved_repo_path="/workspace/myrepo",
        )
        without_slash = captured_payload["payload"]["__label__"]

        assert with_slash == without_slash == "plan-myrepo-5"


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


# ──────────────────────────────────────────────────────────────────────
# TARGET_REPO_MISMATCH — suppress selected_repo emission on wrong-repo runs
# ──────────────────────────────────────────────────────────────────────


class TestTargetRepoMismatchSuppression:
    """When Claude Code appends `TARGET_REPO_MISMATCH: true`, the platform
    must NOT emit __context__.selected_repo for this run. Without this
    suppression, the wrong-repo run silently overwrites the prior turn's
    correct selected_repo, and subsequent refinement turns default to
    the wrong repo (cascade error). Bug repro from real chat session
    where Planner refined a lunchy_box_frontend plan, the call defaulted
    to another_agent_frontend's CODING_REPO_PATH, and the resulting
    'wrong repo' response replaced selected_repo with the wrong value."""

    def test_suppresses_context_when_marker_present(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        stub_popen.Popen.return_value = _StubProcCompleted(
            stdout=(
                "## Plan\n\nThis ticket is misfiled — work belongs in "
                "lunchy_box_frontend.\n\n"
                "SUMMARY: zero-step wrong-repo response\n"
                "TARGET_REPO_MISMATCH: true"
            )
        )

        planning_router._run_planning_job(
            job_id="job-x",
            ticket_number=13,
            ticket_body="body",
            resolved_repo_path="/tmp/wrong-repo",
        )

        p = captured_payload["payload"]
        assert "__context__" not in p, (
            "wrong-repo runs MUST NOT emit selected_repo — doing so "
            "overwrites the prior turn's correct selection and corrupts "
            "the conversation's repo state for every subsequent turn"
        )
        # Other fields still present — the run completed, just produced
        # no actionable plan.
        assert p["plan"]  # non-empty (the "wrong repo" explanation)
        assert "TARGET_REPO_MISMATCH" not in p["plan"], (
            "marker must be stripped from the LLM-visible plan field"
        )
        assert p["__label__"] == "plan-wrong-repo-13"  # label still emitted

    def test_emits_context_normally_when_marker_absent(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        # Sanity: the suppression logic only triggers on the marker.
        # Normal plans must still emit selected_repo as before.
        stub_popen.Popen.return_value = _StubProcCompleted(
            stdout="## Plan\n\n1. step one\n2. step two\n\nSUMMARY: real plan"
        )

        planning_router._run_planning_job(
            job_id="job-y",
            ticket_number=14,
            ticket_body="body",
            resolved_repo_path="/tmp/right-repo",
        )

        p = captured_payload["payload"]
        assert "__context__" in p
        assert "selected_repo" in p["__context__"]
        # The autouse _stub_repo_helpers fixture returns a hardcoded
        # context regardless of input path — sanity-check the structure
        # is present rather than the specific name.
        assert p["__context__"]["selected_repo"]["name"] == "fake-repo"

    def test_summary_still_extracted_when_marker_present(
        self, stub_popen, captured_payload: dict[str, Any]
    ) -> None:
        # Even on a wrong-repo response, the SUMMARY line should still
        # parse correctly. The marker strip happens BEFORE the summary
        # split so neither field contaminates the other.
        stub_popen.Popen.return_value = _StubProcCompleted(
            stdout=(
                "Wrong repo explanation.\n\n"
                "SUMMARY: ticket misfiled, recommend re-filing\n"
                "TARGET_REPO_MISMATCH: true"
            )
        )

        planning_router._run_planning_job(
            job_id="job-z",
            ticket_number=13,
            ticket_body="body",
            resolved_repo_path="/tmp/wrong-repo",
        )

        p = captured_payload["payload"]
        assert p["__summary__"] == "ticket misfiled, recommend re-filing"
