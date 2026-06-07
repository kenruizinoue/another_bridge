from importlib.metadata import PackageNotFoundError, version as _pkg_version

from fastapi import APIRouter, Request

from services.session_store import session_store

router = APIRouter()


def _project_version() -> str:
    # importlib.metadata reads the installed package metadata, so
    # pyproject.toml stays the single source of truth — bump the
    # version there and /health updates without any code change.
    # Falls back to "unknown" when the bridge runs from a clone
    # without `pip install` (uptime checks must not 500).
    try:
        return _pkg_version("another_bridge")
    except PackageNotFoundError:
        return "unknown"


# Used when /health is hit on an app whose lifespan never ran
# (typical for the focused TestClient apps in tests/). The boot-time
# probe is the only producer in production, so "probe not run" is an
# honest report rather than a guess.
_PROBE_NOT_RUN = {"ok": False, "detail": "probe not run"}


@router.get("/health")
def health(request: Request):
    probe = getattr(request.app.state, "claude_probe", None) or _PROBE_NOT_RUN
    return {
        "ok": True,
        "service": "another_bridge",
        "version": _project_version(),
        "claude_probe": probe,
        "session_store_reachable": session_store.is_reachable(),
    }
