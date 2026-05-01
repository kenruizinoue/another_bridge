from importlib.metadata import PackageNotFoundError, version as _pkg_version

from fastapi import APIRouter

router = APIRouter()


def _project_version() -> str:
    # importlib.metadata reads the installed package metadata, so
    # pyproject.toml stays the single source of truth — bump the
    # version there and /health updates without any code change.
    # Falls back to "unknown" when the bridge runs from a clone
    # without `pip install` (uptime checks must not 500).
    try:
        return _pkg_version("another_coder")
    except PackageNotFoundError:
        return "unknown"


@router.get("/health")
def health():
    return {
        "ok": True,
        "service": "another_coder",
        "version": _project_version(),
    }
