"""Structured failure kinds for the webhook + bridge runners.

Replaces the stringly-typed ``mark_failed("...")`` calls. Every
runner (planning, implementation, chat_stream) can now classify
its failure by kind — spawn / timeout / cancel / claude / push /
pr — and the JobManager surfaces it alongside the human-readable
message at ``GET /jobs/<id>/status``.

The platform's poller (executeTool.dispatch.ts on the backend
side, the trace drawer on the frontend) doesn't read ``error_kind``
yet — it still renders ``error`` verbatim. Adding the structure
now is forward-compatible: when a future platform change wants
to render "Bridge couldn't reach the model" differently from
"GitHub PR creation rate-limited", the data is already there.
Until then this is pure metadata + log enrichment, no runtime
behavior change.

Why not exceptions? The runners catch their own exceptions
locally — the runner ALREADY decides whether the run is a
spawn failure vs a non-zero exit vs a cancel, by inspecting the
``ClaudeResult`` shape. Replacing those branches with raise+catch
would just shuffle code around. Treating the kind as a value
keeps the runner readable.
"""

from __future__ import annotations

from typing import Final


# String constants instead of an enum because the values appear
# verbatim in the JSON status response and getting an enum to
# serialize cleanly through job_manager.to_status_response would
# cost more than this saves. Keep them lowercase + snake_case so
# they're stable wire identifiers.

#: Claude binary missing on PATH (or wherever ``CLAUDE_BIN_PATH``
#: points). Distinct from claude_failed because the user fix is
#: different — operator sets CLAUDE_BIN_PATH vs. agent retries.
SPAWN_FAILED: Final[str] = "spawn_failed"

#: ``run_blocking`` hit its timeout budget. The subprocess was
#: killed by communicate's timeout handler. Caller can retry
#: (planner/implementer) with a longer budget if they have one.
TIMEOUT: Final[str] = "timeout"

#: External cancel arrived during the run — POST /jobs/<id>/cancel
#: from the platform OR is_cancelled flipped before subprocess
#: exit. Distinct from claude_failed because the user did NOT
#: see a finished result; UI should reflect "cancelled" not
#: "errored".
CANCELLED: Final[str] = "cancelled"

#: Claude exited with non-zero return code. The runner has Claude's
#: stderr (or "exited with code N" when stderr was empty) in the
#: ``error`` message. Most actionable failure for an LLM-driven
#: retry — the next prompt iteration can read the stderr verbatim.
CLAUDE_FAILED: Final[str] = "claude_failed"

#: ``git push`` failed after a successful Claude run — auth, network,
#: or branch-protection rule. Distinct from claude_failed so the
#: UI doesn't blame the model for an infrastructure problem.
GIT_PUSH_FAILED: Final[str] = "git_push_failed"

#: GitHub PR creation API call failed — usually a 422 (head invalid /
#: same head + base) or a 401/403 (PAT missing scope). The
#: ``error`` carries the enriched message from
#: ``services.github_service.format_pr_creation_error``.
PR_CREATE_FAILED: Final[str] = "pr_create_failed"


# Convenience set for tests + introspection. Anything ``mark_failed``
# accepts as ``kind`` should appear here.
ALL_KINDS: Final[frozenset[str]] = frozenset(
    {
        SPAWN_FAILED,
        TIMEOUT,
        CANCELLED,
        CLAUDE_FAILED,
        GIT_PUSH_FAILED,
        PR_CREATE_FAILED,
    }
)
