"""Claude CLI subprocess lifecycle abstraction.

Provides two seams used by every router that spawns `claude`:

  - run_blocking: capture-all-output + timeout + cancel-aware. Used by
    instruct_planning and instruct_implementation, which need the full
    text response back as one string.

  - streaming_subprocess: context manager that spawns claude with
    line-buffered stdout, attaches the proc to JobManager so cancel can
    SIGTERM, yields the live Popen, and detaches on exit. Used by
    /chat/stream, which iterates stdout JSONL line by line.

Both seams attach + detach via JobManager so /jobs/<id>/cancel routes
to the right subprocess group regardless of which path spawned it.
Both also set start_new_session=True so killpg reaches descendants
(git, file tools, mcp servers) that Claude Code spawns.

Tests patch this module's `run_blocking` / `streaming_subprocess` rather
than poking subprocess directly — keeps callers' subprocess use opaque.
"""

from __future__ import annotations

import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from jobs import job_manager


@dataclass
class ClaudeResult:
    """Outcome of a blocking claude run. Discriminated by which fields
    are populated:
      - spawn_error set: claude binary missing / OSError on Popen
      - timed_out True: communicate hit the timeout (process killed)
      - cancelled True: external cancel landed during the run
      - returncode == 0 and none of the above: clean run, stdout is the answer
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    cancelled: bool
    duration_seconds: float
    spawn_error: str | None = None


def build_claude_args(
    prompt: str,
    model: str,
    output_format: str = "text",
    extra_flags: list[str] | None = None,
) -> list[str]:
    """Centralized claude CLI arg construction. Always includes
    --dangerously-skip-permissions because every coder caller runs
    headless from a webhook — the user can't approve permission
    prompts from the platform UI.
    """
    args = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        output_format,
        "--dangerously-skip-permissions",
    ]
    if extra_flags:
        args.extend(extra_flags)
    return args


def run_blocking(
    args: list[str],
    cwd: str,
    timeout_seconds: int,
    job_id: str,
) -> ClaudeResult:
    """Spawn claude, attach to job_manager, communicate with timeout,
    return the captured output. Caller dispatches on the result fields:
    spawn_error / timed_out / cancelled / returncode.

    start_new_session=True puts claude in its own process group so
    JobManager.cancel can killpg the whole tree (git, mcp servers, etc).
    """
    started = time.time()
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
    except FileNotFoundError as err:
        return ClaudeResult(
            returncode=-1,
            stdout="",
            stderr="",
            timed_out=False,
            cancelled=False,
            duration_seconds=round(time.time() - started, 2),
            spawn_error=f"failed to spawn claude: {err}",
        )

    job_manager.attach_process(job_id, proc)
    timed_out = False
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            timed_out = True
    finally:
        job_manager.detach_process(job_id)

    return ClaudeResult(
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
        cancelled=job_manager.is_cancelled(job_id),
        duration_seconds=round(time.time() - started, 2),
    )


@contextmanager
def streaming_subprocess(
    args: list[str],
    cwd: str,
    job_id: str,
) -> Iterator[subprocess.Popen]:
    """Spawn claude with line-buffered stdout for callers that need to
    iterate output as it arrives (chat_stream's stream-json events).
    Attaches to JobManager + detaches on exit; caller is responsible
    for waiting on the proc and reading stdout/stderr.

    Re-raises FileNotFoundError so the caller can emit a domain-specific
    SSE error event; everything else is the caller's problem.
    """
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Line buffered so JSONL lines flush as Claude emits them
        # instead of pooling into a 4KB block. Without this, the
        # browser sees no SSE chunks until ~4KB of text accumulates.
        bufsize=1,
        cwd=cwd,
        start_new_session=True,
    )
    job_manager.attach_process(job_id, proc)
    try:
        yield proc
    finally:
        job_manager.detach_process(job_id)
