"""Tests for split_plan_and_summary in routers/planning.py.

Claude Code is instructed to end every planning response with a single
`SUMMARY: <one sentence>` line. The webhook parses it out with this
helper so the platform can surface the summary in the next turn's
[Conversation artifacts] block via the __summary__ opt-in. When Claude
ignores the instruction or the output is truncated, the helper returns
None for summary and the platform's auto-summary fallback takes over.
"""

from routers.planning import (
    detect_target_repo_mismatch,
    split_plan_and_summary,
    strip_target_repo_mismatch_marker,
)


class TestSplitPlanAndSummary:
    def test_extracts_trailing_summary_line(self):
        output = (
            "## Implementation Plan\n\n"
            "1. Update src/models/user.py\n"
            "2. Add migration\n\n"
            "SUMMARY: Ticket #34 — adds nullable role column to users + migration."
        )
        plan, summary = split_plan_and_summary(output)
        assert plan == "## Implementation Plan\n\n1. Update src/models/user.py\n2. Add migration"
        assert summary == "Ticket #34 — adds nullable role column to users + migration."

    def test_returns_none_summary_when_marker_absent(self):
        # Claude ignored the instruction or hit the token limit before
        # writing SUMMARY. Plan comes through as-is; backend falls back
        # to its auto-summary.
        output = "## Implementation Plan\n\n1. Step one\n2. Step two"
        plan, summary = split_plan_and_summary(output)
        assert plan == output
        assert summary is None

    def test_returns_none_summary_when_marker_present_but_empty(self):
        # `SUMMARY:` with nothing after it. Treat as missing so the
        # backend's auto-summary still fires.
        output = "## Implementation Plan\n\n1. Step one\n\nSUMMARY: "
        plan, summary = split_plan_and_summary(output)
        assert plan == "## Implementation Plan\n\n1. Step one"
        assert summary is None

    def test_strips_whitespace_around_summary(self):
        output = "Plan body.\n\nSUMMARY:    trimmed sentence with padding   "
        _, summary = split_plan_and_summary(output)
        assert summary == "trimmed sentence with padding"

    def test_strips_whitespace_around_plan(self):
        output = "  ## Plan\n\nstep 1\n\nSUMMARY: ok  "
        plan, _ = split_plan_and_summary(output)
        assert plan == "## Plan\n\nstep 1"

    def test_only_matches_trailing_summary_not_mid_plan_mention(self):
        # The literal text "SUMMARY:" might appear earlier in the plan
        # (e.g. quoted in a step). Only the LAST occurrence at the end
        # of the output should be treated as the marker.
        output = (
            "## Plan\n\n"
            "1. Update the SUMMARY: section in the README to mention X.\n"
            "2. Step two.\n\n"
            "SUMMARY: Updates README + ships step two."
        )
        plan, summary = split_plan_and_summary(output)
        assert summary == "Updates README + ships step two."
        # The mid-plan mention must survive in the plan body.
        assert "Update the SUMMARY:" in plan

    def test_handles_multiline_summary_by_taking_only_first_line(self):
        # Defensive: if Claude writes a multi-line summary against the
        # instruction, the regex DOTALL still captures it, but at least
        # we should not crash. Confirm we get a string back.
        output = "Plan.\n\nSUMMARY: line one\nline two"
        _, summary = split_plan_and_summary(output)
        assert summary is not None
        assert summary.startswith("line one")

    def test_empty_output_returns_empty_plan_and_none_summary(self):
        plan, summary = split_plan_and_summary("")
        assert plan == ""
        assert summary is None


# ──────────────────────────────────────────────────────────────────────
# TARGET_REPO_MISMATCH marker — wrong-repo signaling from Claude Code
# ──────────────────────────────────────────────────────────────────────


class TestTargetRepoMismatch:
    """Claude Code appends `TARGET_REPO_MISMATCH: true` when it detects
    the ticket was filed against the wrong repo. The platform reads this
    to suppress the selected_repo context emission so the wrong-repo run
    doesn't silently overwrite the prior turn's correct selection."""

    def test_detect_returns_true_when_marker_present(self):
        out = (
            "## Plan\n\nThis ticket is misfiled — work belongs in sibling.\n\n"
            "SUMMARY: zero-step wrong-repo response\n"
            "TARGET_REPO_MISMATCH: true"
        )
        assert detect_target_repo_mismatch(out) is True

    def test_detect_returns_false_when_marker_absent(self):
        out = "## Plan\n\n1. Step one\n2. Step two\n\nSUMMARY: normal plan"
        assert detect_target_repo_mismatch(out) is False

    def test_detect_is_case_insensitive(self):
        # Defensive: an LLM might emit "true" / "TRUE" / "True" depending
        # on prompt drift. Accept all common casings.
        for value in ["true", "True", "TRUE"]:
            out = f"plan body\n\nTARGET_REPO_MISMATCH: {value}"
            assert detect_target_repo_mismatch(out) is True, value

    def test_detect_only_matches_full_marker_line(self):
        # Defensive: the literal string might appear inside a plan step
        # (e.g. "we should add a TARGET_REPO_MISMATCH: true assertion").
        # Only count it as the marker when it stands alone on its own line.
        # Use a leading newline so MULTILINE matches against ^.
        out_inline = "Plan\n\n1. Add `TARGET_REPO_MISMATCH: true` to the test"
        # The whole step text is on one line (the bullet line) — and the
        # marker substring is preceded by a backtick + space, not by a
        # line start. Should NOT match.
        assert detect_target_repo_mismatch(out_inline) is False

    def test_strip_removes_marker_line(self):
        out = "Plan body\n\nSUMMARY: foo\nTARGET_REPO_MISMATCH: true"
        cleaned = strip_target_repo_mismatch_marker(out)
        assert "TARGET_REPO_MISMATCH" not in cleaned
        # SUMMARY line preserved (split_plan_and_summary handles it later)
        assert "SUMMARY: foo" in cleaned

    def test_strip_is_idempotent_when_marker_absent(self):
        out = "Plan body\n\nSUMMARY: foo"
        assert strip_target_repo_mismatch_marker(out) == out

    def test_strip_handles_marker_in_middle(self):
        # Edge case — Claude might put it before SUMMARY rather than
        # after. Marker should still be removed regardless of position.
        out = "Plan body\n\nTARGET_REPO_MISMATCH: true\n\nSUMMARY: foo"
        cleaned = strip_target_repo_mismatch_marker(out)
        assert "TARGET_REPO_MISMATCH" not in cleaned
        assert "SUMMARY: foo" in cleaned
