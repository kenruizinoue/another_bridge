"""Webhook payload schemas for the /tools/instruct_* endpoints.

The platform's webhook dispatcher (another_agent_backend's
executeTool.dispatch.ts) uses ``response.ok`` to decide whether the
body is "an error from the webhook" vs "a tool result". Returning
FastAPI's default 422 on validation failure would break that contract
— the LLM would see "webhook returned 422 Unprocessable Entity" and
lose every actionable hint about what to fix on the next attempt.

So these schemas validate aggressively (size caps, type coercion) but
the route handlers translate any ValidationError back to the legacy
``{"error": "<friendly hint>"}`` shape with status 200, which the
platform passes through to the LLM verbatim. ``model_validator``
fires before pydantic's own field-level errors so the empty/missing
cases produce the recovery hints the Planner Agent reads.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

# Sized to leave generous headroom over real-world inputs without
# inviting unbounded payloads. ticket_body is a GitHub issue body
# (largest seen ~12 KB); plan is multi-section and may include code
# snippets but is excerpted to 4000 chars before the prompt anyway.
TICKET_BODY_MAX = 50_000
PLAN_MAX = 200_000
REPO_PATH_MAX = 4_096
BASE_BRANCH_MAX = 255

# Hard cap on the ``timeout_seconds`` override callers can request.
# Mirrors the module-level PLANNING_TIMEOUT_SECONDS / IMPLEMENTATION_-
# TIMEOUT_SECONDS in the runners. The platform's per-tool
# pollMaxSeconds is sized to this value, so a longer subprocess budget
# would just produce results the platform poller has already given up
# on. Lower budgets are fine — the platform doesn't mind early
# completion.
TIMEOUT_SECONDS_MAX = 1800

# LLM-recovery hint reused verbatim across both planning and
# implementation. The agent reads this message from the failed-tool
# result and rebuilds ticket_body from the issue title + user
# description on the next pass, instead of bailing.
EMPTY_TICKET_BODY_HINT = (
    "ticket_body is required and must be a non-empty string. "
    "If the GitHub issue body is empty, build ticket_body from "
    "the issue title plus the user's description in the chat "
    "(do not pass empty)."
)
TICKET_NUMBER_HINT = "ticket_number is required and must be an integer"
PLAN_HINT = "plan is required and must be a non-empty string"


def _validate_ticket_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Common pre-validation for ticket_number + ticket_body. Both
    planning and implementation need the exact same friendly messages
    here, so it lives in one place."""
    tn = data.get("ticket_number")
    if tn is None:
        raise PydanticCustomError("value_error", TICKET_NUMBER_HINT)
    try:
        data["ticket_number"] = int(tn)
    except (TypeError, ValueError):
        raise PydanticCustomError("value_error", TICKET_NUMBER_HINT)

    tb = data.get("ticket_body")
    if not isinstance(tb, str) or not tb.strip():
        raise PydanticCustomError("value_error", EMPTY_TICKET_BODY_HINT)
    return data


class PlanningRequest(BaseModel):
    """Body shape for POST /tools/instruct_planning.

    ``repo_path`` is optional because the handler falls back to
    ``CODING_REPO_PATH`` and then runs ``validate_repo_path`` for the
    workspace-root + symlink-escape checks — keeping that flow at the
    route level since it depends on env config, not request shape.

    ``timeout_seconds`` lets the platform request a shorter budget
    for trivial tickets without forking the runner constants. None /
    omitted → use the runner's PLANNING_TIMEOUT_SECONDS default.
    Capped at TIMEOUT_SECONDS_MAX = 1800 to match the platform's
    poll budget; ``ge=1`` rejects zero/negative.
    """

    ticket_number: int
    ticket_body: str = Field(min_length=1, max_length=TICKET_BODY_MAX)
    repo_path: str | None = Field(default=None, max_length=REPO_PATH_MAX)
    timeout_seconds: int | None = Field(
        default=None, ge=1, le=TIMEOUT_SECONDS_MAX,
    )

    @model_validator(mode="before")
    @classmethod
    def _validate_required(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return _validate_ticket_fields(data)


class ImplementationRequest(BaseModel):
    """Body shape for POST /tools/instruct_implementation.

    ``base_branch`` is an optional override; when omitted, the runner's
    ``_resolve_base_branch`` auto-detects the remote's default branch
    via ``ls-remote --symref``. Empty strings are coerced to None at
    validation time so the override is genuinely opt-in.

    ``timeout_seconds`` mirrors PlanningRequest — same cap + same
    "None means use the runner default" semantics.
    """

    ticket_number: int
    ticket_body: str = Field(min_length=1, max_length=TICKET_BODY_MAX)
    plan: str = Field(min_length=1, max_length=PLAN_MAX)
    repo_path: str | None = Field(default=None, max_length=REPO_PATH_MAX)
    base_branch: str | None = Field(default=None, max_length=BASE_BRANCH_MAX)
    timeout_seconds: int | None = Field(
        default=None, ge=1, le=TIMEOUT_SECONDS_MAX,
    )

    @model_validator(mode="before")
    @classmethod
    def _validate_required(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = _validate_ticket_fields(data)
        plan = data.get("plan")
        if not isinstance(plan, str) or not plan.strip():
            raise PydanticCustomError("value_error", PLAN_HINT)
        # Treat empty/whitespace-only base_branch as "not provided" so
        # the auto-detection path runs. The legacy handler did the
        # same coercion inline.
        bb = data.get("base_branch")
        if isinstance(bb, str) and not bb.strip():
            data["base_branch"] = None
        return data


def first_error_message(e: ValidationError) -> str:
    """Pydantic accumulates every error in a list; the legacy handlers
    returned on the first failure, so we keep that shape — one
    actionable hint per response. Non-PydanticCustomError messages
    (e.g. max_length violations) get a ``<field>: <msg>`` prefix so
    the LLM can tell which field broke."""
    err = e.errors()[0]
    msg = err.get("msg", "validation failed")
    # PydanticCustomError surfaces the raw message verbatim; standard
    # errors ("String should have at most N characters") do not, so
    # prefix those with the field name. ``loc`` is empty when the
    # error came from a model_validator, non-empty when it came from
    # a field validator or constraint.
    loc = err.get("loc") or ()
    if not loc:
        return msg
    field = ".".join(str(p) for p in loc)
    return f"{field}: {msg}"
