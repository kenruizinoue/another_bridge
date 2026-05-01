"""Coverage for two related contracts:

  1. Structured ``error_kind`` on JobManager.mark_failed surfaces
     through to ``to_status_response``. Locks the wire contract
     that the platform's poller can branch on (forward-compatible
     — no platform change required today).

  2. ``timeout_seconds`` override on PlanningRequest +
     ImplementationRequest is accepted, capped at 1800, and rejects
     zero/negative.

The runner-level wiring (planning/implementation passing the value
through to claude_runner.run_blocking) is covered indirectly by the
schema tests + the existing route tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobs import JobManager
from routers._schemas import (
    ImplementationRequest,
    PlanningRequest,
    TIMEOUT_SECONDS_MAX,
    first_error_message,
)
from services.errors import (
    ALL_KINDS,
    CANCELLED,
    CLAUDE_FAILED,
    GIT_PUSH_FAILED,
    PR_CREATE_FAILED,
    SPAWN_FAILED,
    TIMEOUT,
)


# ──────────────────────────────────────────────────────────────────────
# JobManager.mark_failed kind plumbing
# ──────────────────────────────────────────────────────────────────────


class TestMarkFailedKind:
    def test_mark_failed_with_kind_surfaces_in_status_response(self) -> None:
        mgr = JobManager()
        job = mgr.create("instruct_planning")
        mgr.mark_failed(job.job_id, "claude blew up", kind=CLAUDE_FAILED)

        body = mgr.get(job.job_id).to_status_response()  # type: ignore[union-attr]
        assert body["status"] == "failed"
        assert body["error"] == "claude blew up"
        assert body["error_kind"] == "claude_failed"

    def test_mark_failed_without_kind_omits_error_kind_field(self) -> None:
        # Backward-compat: callers that haven't been updated yet (or
        # generic git failures that don't map to a canonical kind)
        # produce a status response WITHOUT error_kind. Locks that an
        # older platform consumer keeps working.
        mgr = JobManager()
        job = mgr.create("instruct_implementation")
        mgr.mark_failed(job.job_id, "working tree dirty")

        body = mgr.get(job.job_id).to_status_response()  # type: ignore[union-attr]
        assert "error_kind" not in body
        assert body["error"] == "working tree dirty"

    def test_external_cancel_overrides_runner_kind(self) -> None:
        # When cancel was flipped externally before the runner called
        # mark_failed, the cancel reason wins regardless of what the
        # runner classified its error as. The user already gave up;
        # the underlying error is downstream noise.
        mgr = JobManager()
        job = mgr.create("instruct_planning")
        # Simulate an external cancel landing first.
        with mgr._lock:  # type: ignore[attr-defined]
            mgr._jobs[job.job_id].cancelled = True  # type: ignore[attr-defined]
        # Runner then reports a different kind — should be ignored.
        mgr.mark_failed(job.job_id, "claude exited 137", kind=CLAUDE_FAILED)

        body = mgr.get(job.job_id).to_status_response()  # type: ignore[union-attr]
        assert body["error"] == "cancelled by client"
        assert body["error_kind"] == "cancelled"

    def test_all_canonical_kinds_round_trip(self) -> None:
        # Belt-and-suspenders: every kind constant in ALL_KINDS is
        # storable + readable. Catches a future kind that's added to
        # services/errors.py but forgotten in JobManager's plumbing.
        mgr = JobManager()
        for kind in sorted(ALL_KINDS):
            job = mgr.create("test")
            mgr.mark_failed(job.job_id, f"failure of kind {kind}", kind=kind)
            body = mgr.get(job.job_id).to_status_response()  # type: ignore[union-attr]
            assert body["error_kind"] == kind, f"kind {kind} round-trip failed"


# ──────────────────────────────────────────────────────────────────────
# PlanningRequest / ImplementationRequest timeout_seconds
# ──────────────────────────────────────────────────────────────────────


class TestTimeoutSecondsOverride:
    def _planning_args(self, **overrides):
        base = {"ticket_number": 1, "ticket_body": "fix it"}
        base.update(overrides)
        return base

    def _impl_args(self, **overrides):
        base = {
            "ticket_number": 1,
            "ticket_body": "fix it",
            "plan": "1. step",
        }
        base.update(overrides)
        return base

    def test_planning_accepts_override(self) -> None:
        req = PlanningRequest.model_validate(
            self._planning_args(timeout_seconds=300),
        )
        assert req.timeout_seconds == 300

    def test_planning_default_is_none(self) -> None:
        # None means "use runner default" — the runner falls back to
        # PLANNING_TIMEOUT_SECONDS when the override is None.
        req = PlanningRequest.model_validate(self._planning_args())
        assert req.timeout_seconds is None

    def test_planning_caps_at_1800(self) -> None:
        # Hard cap matches the platform's per-tool pollMaxSeconds.
        # Anything longer would produce results the platform poller
        # has already given up on.
        with pytest.raises(ValidationError) as exc:
            PlanningRequest.model_validate(
                self._planning_args(timeout_seconds=TIMEOUT_SECONDS_MAX + 1),
            )
        msg = first_error_message(exc.value)
        assert "timeout_seconds" in msg

    def test_planning_rejects_zero_and_negative(self) -> None:
        for bad in (0, -1, -300):
            with pytest.raises(ValidationError):
                PlanningRequest.model_validate(
                    self._planning_args(timeout_seconds=bad),
                )

    def test_implementation_accepts_override(self) -> None:
        req = ImplementationRequest.model_validate(
            self._impl_args(timeout_seconds=600),
        )
        assert req.timeout_seconds == 600

    def test_implementation_caps_at_1800(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ImplementationRequest.model_validate(
                self._impl_args(timeout_seconds=TIMEOUT_SECONDS_MAX + 1),
            )
        assert "timeout_seconds" in first_error_message(exc.value)

    def test_implementation_at_exact_cap_is_accepted(self) -> None:
        # ``le`` (less-than-or-equal) is inclusive — 1800 should pass.
        req = ImplementationRequest.model_validate(
            self._impl_args(timeout_seconds=TIMEOUT_SECONDS_MAX),
        )
        assert req.timeout_seconds == TIMEOUT_SECONDS_MAX
