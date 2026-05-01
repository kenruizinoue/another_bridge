"""Schemas + integration coverage for the /tools/instruct_* webhooks.

Locks two contracts that the platform's webhook dispatcher
(another_agent_backend's executeTool.dispatch.ts) depends on:

  1. Validation failures return HTTP 200 with a JSON body of shape
     ``{"error": "<friendly hint>"}``. The dispatcher's
     ``response.ok`` check would reject anything 4xx and the LLM
     would only see ``webhook returned 422 Unprocessable Entity`` —
     losing the actionable recovery hints the Planner Agent reads to
     self-correct on its next pass.

  2. The recovery hint for an empty ``ticket_body`` MUST tell the
     agent how to recover (synthesize body from the issue title +
     chat description). This is load-bearing: a generic
     "ticket_body required" leaves the agent stuck.

Schema-level cases are unit-tested directly; the route-level wiring
is covered with a TestClient because the response-shape contract is
what the platform actually consumes.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from routers._schemas import (
    EMPTY_TICKET_BODY_HINT,
    ImplementationRequest,
    PLAN_HINT,
    PLAN_MAX,
    PlanningRequest,
    TICKET_BODY_MAX,
    TICKET_NUMBER_HINT,
    first_error_message,
)


# ──────────────────────────────────────────────────────────────────────
# PlanningRequest — schema-level
# ──────────────────────────────────────────────────────────────────────


class TestPlanningRequestSchema:
    def test_accepts_minimal_valid_input(self) -> None:
        req = PlanningRequest.model_validate(
            {"ticket_number": 42, "ticket_body": "Fix the bug."},
        )
        assert req.ticket_number == 42
        assert req.ticket_body == "Fix the bug."
        assert req.repo_path is None

    def test_coerces_string_ticket_number(self) -> None:
        # Some LLMs stringify numbers in tool calls; the legacy handler
        # ran int(raw) defensively, so the schema must too.
        req = PlanningRequest.model_validate(
            {"ticket_number": "42", "ticket_body": "x"},
        )
        assert req.ticket_number == 42

    def test_missing_ticket_number_produces_friendly_hint(self) -> None:
        with pytest.raises(ValidationError) as exc:
            PlanningRequest.model_validate({"ticket_body": "x"})
        assert first_error_message(exc.value) == TICKET_NUMBER_HINT

    def test_non_int_ticket_number_produces_friendly_hint(self) -> None:
        with pytest.raises(ValidationError) as exc:
            PlanningRequest.model_validate(
                {"ticket_number": "not-a-number", "ticket_body": "x"},
            )
        assert first_error_message(exc.value) == TICKET_NUMBER_HINT

    def test_empty_ticket_body_produces_recovery_hint(self) -> None:
        # Load-bearing: the LLM reads this exact phrasing and uses it
        # to synthesize a body from the issue title + chat description
        # on the next attempt. Generic "required" wouldn't be enough.
        with pytest.raises(ValidationError) as exc:
            PlanningRequest.model_validate(
                {"ticket_number": 1, "ticket_body": "   "},
            )
        assert first_error_message(exc.value) == EMPTY_TICKET_BODY_HINT

    def test_missing_ticket_body_produces_recovery_hint(self) -> None:
        with pytest.raises(ValidationError) as exc:
            PlanningRequest.model_validate({"ticket_number": 1})
        assert first_error_message(exc.value) == EMPTY_TICKET_BODY_HINT

    def test_oversized_ticket_body_is_rejected(self) -> None:
        # The actual gap closed by this refactor — inbound payloads
        # were uncapped, so a 1MB body would silently slow the
        # planning prompt build before the runner truncated it.
        with pytest.raises(ValidationError) as exc:
            PlanningRequest.model_validate(
                {"ticket_number": 1, "ticket_body": "x" * (TICKET_BODY_MAX + 1)},
            )
        msg = first_error_message(exc.value)
        # max_length errors come from pydantic's standard machinery,
        # so they get the "<field>: <msg>" prefix from the mapper.
        assert msg.startswith("ticket_body:")


# ──────────────────────────────────────────────────────────────────────
# ImplementationRequest — schema-level
# ──────────────────────────────────────────────────────────────────────


class TestImplementationRequestSchema:
    def _valid_args(self, **overrides):
        base = {
            "ticket_number": 1,
            "ticket_body": "fix it",
            "plan": "1. Do the thing.",
        }
        base.update(overrides)
        return base

    def test_accepts_minimal_valid_input(self) -> None:
        req = ImplementationRequest.model_validate(self._valid_args())
        assert req.plan == "1. Do the thing."
        assert req.base_branch is None

    def test_empty_plan_produces_hint(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ImplementationRequest.model_validate(self._valid_args(plan="  "))
        assert first_error_message(exc.value) == PLAN_HINT

    def test_oversized_plan_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ImplementationRequest.model_validate(
                self._valid_args(plan="x" * (PLAN_MAX + 1)),
            )
        assert first_error_message(exc.value).startswith("plan:")

    def test_empty_base_branch_string_is_normalized_to_none(self) -> None:
        # The legacy handler did this coercion inline; preserved in the
        # schema so the auto-detection path in _resolve_base_branch
        # runs whenever the platform passes "" (which the platform
        # does when the LLM omits the override).
        req = ImplementationRequest.model_validate(self._valid_args(base_branch="   "))
        assert req.base_branch is None

    def test_explicit_base_branch_passthrough(self) -> None:
        req = ImplementationRequest.model_validate(
            self._valid_args(base_branch="main"),
        )
        assert req.base_branch == "main"


# ──────────────────────────────────────────────────────────────────────
# Route-level: response shape stays 200 + {"error": "..."} on failure
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    """Mount the routers in isolation so we don't drag in the auth
    dependency (these tests are about the validation contract, not
    the bridge-key gate). Mirrors the pattern in test_chat_polling.py."""
    from routers import implementation as impl_router
    from routers import planning as plan_router

    app = FastAPI()
    app.include_router(plan_router.router)
    app.include_router(impl_router.router)
    return TestClient(app)


class TestPlanningWebhookResponseShape:
    def test_validation_failure_returns_200_with_error_string(
        self, client: TestClient
    ) -> None:
        # Critical: NOT 422. The platform's webhook dispatcher uses
        # response.ok to decide whether to surface the body to the
        # LLM. A 4xx here loses the recovery hint and the LLM only
        # sees "webhook returned 422 Unprocessable Entity".
        resp = client.post(
            "/tools/instruct_planning",
            json={"ticket_number": 1, "ticket_body": ""},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"error": EMPTY_TICKET_BODY_HINT}

    def test_envelope_form_is_unwrapped(self, client: TestClient) -> None:
        # The platform's dispatcher sends the args under an
        # ``arguments`` key; extract_args has to unwrap before the
        # schema sees them. Both shapes must produce the same hint.
        resp = client.post(
            "/tools/instruct_planning",
            json={"arguments": {"ticket_number": "x", "ticket_body": "y"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"error": TICKET_NUMBER_HINT}

    def test_oversized_body_returns_field_prefixed_error(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/tools/instruct_planning",
            json={"ticket_number": 1, "ticket_body": "x" * (TICKET_BODY_MAX + 1)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body
        assert body["error"].startswith("ticket_body:")


class TestImplementationWebhookResponseShape:
    def test_validation_failure_returns_200_with_error_string(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/tools/instruct_implementation",
            json={"ticket_number": 1, "ticket_body": "x", "plan": ""},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"error": PLAN_HINT}

    def test_unbounded_plan_capped(self, client: TestClient) -> None:
        # Real bug closed by this refactor — a 1MB plan body would
        # silently slow the implementation prompt build (the runner's
        # plan_excerpt truncates to 4000 chars but happens AFTER the
        # body has been parsed and held in memory).
        resp = client.post(
            "/tools/instruct_implementation",
            json={
                "ticket_number": 1,
                "ticket_body": "x",
                "plan": "x" * (PLAN_MAX + 1),
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"].startswith("plan:")
