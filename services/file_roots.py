"""Read-only file browsing over named roots, for the phone's Files view.

The server owns the roots. `ANOTHER_BRIDGE_FILE_ROOTS` is a comma list of
`name=folder` pairs, like `outputs=~/agent_outputs`; each folder must be
an existing folder inside the home folder (not home itself). The phone
only ever sends a root name and a path relative to it, so it never sees
or picks an absolute path. Adding a folder for another use is one more
pair in `.env` and a restart, with no app change.

Nothing outside a root is reachable: paths are resolved (symlinks
included) and must stay inside the root's real path. Hidden entries
(any part starting with a dot) and key-like files are never listed or
served.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

MAX_ENTRIES = 500

# Key and credential files are never shared, even inside a root.
_SECRET_SUFFIXES = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".mobileprovision", ".p8"}
)

_KINDS: dict[str, tuple[str, str]] = {}


def _register(kind: str, media_type: str, *exts: str) -> None:
    for ext in exts:
        _KINDS[ext] = (kind, media_type)


_register("video", "video/mp4", "mp4")
_register("video", "video/quicktime", "mov")
_register("video", "video/x-m4v", "m4v")
_register("image", "image/png", "png")
_register("image", "image/jpeg", "jpg", "jpeg")
_register("image", "image/gif", "gif")
_register("image", "image/webp", "webp")
_register("image", "image/heic", "heic")
_register("audio", "audio/mpeg", "mp3")
_register("audio", "audio/mp4", "m4a")
_register("audio", "audio/wav", "wav")
_register("audio", "audio/aac", "aac")
_register("pdf", "application/pdf", "pdf")
# Text of any flavor is served as plain text, so nothing renders as a page.
_register(
    "text",
    "text/plain; charset=utf-8",
    "txt", "md", "markdown", "json", "csv", "tsv", "log", "srt", "vtt",
    "yaml", "yml", "toml", "xml", "html", "css", "js", "ts", "tsx", "jsx",
    "py", "sh", "sql", "ini", "cfg",
)
_OTHER = ("other", "application/octet-stream")


class FileRootsConfigError(ValueError):
    """`ANOTHER_BRIDGE_FILE_ROOTS` is invalid. Raised at boot."""


class NotFound(LookupError):
    pass


class NotAllowed(PermissionError):
    pass


@dataclass(frozen=True)
class FileRoot:
    name: str
    path: Path  # resolved real path
    label: str


def parse_roots(spec: str, home: Path | None = None) -> list[FileRoot]:
    home_real = (home or Path.home()).resolve()
    roots: list[FileRoot] = []
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        name, sep, folder = (part.strip() for part in item.partition("="))
        if not sep or not folder:
            raise FileRootsConfigError(f"file root {item!r} needs the form name=folder")
        if not _NAME.match(name):
            raise FileRootsConfigError(f"file root name {name!r} must be a lowercase slug")
        if any(r.name == name for r in roots):
            raise FileRootsConfigError(f"file root name {name!r} is used twice")
        if folder == "~" or folder.startswith("~/"):
            folder = str(home_real) + folder[1:]
        path = Path(folder).resolve()
        if not path.is_dir():
            raise FileRootsConfigError(f"file root {name!r} is not an existing folder")
        if path == home_real or not path.is_relative_to(home_real):
            raise FileRootsConfigError(f"file root {name!r} must be a folder inside the home folder")
        roots.append(FileRoot(name=name, path=path, label=path.name))
    return roots


def kind_of(name: str) -> tuple[str, str]:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _KINDS.get(ext, _OTHER)


def _is_hidden(name: str) -> bool:
    return name.startswith(".") or Path(name).suffix.lower() in _SECRET_SUFFIXES


class FileBrowser:
    def __init__(self, roots: list[FileRoot], home: Path | None = None) -> None:
        self._roots = {r.name: r for r in roots}
        self._home = str(home or Path.home())

    def roots(self) -> list[dict[str, str]]:
        return [{"name": r.name, "label": r.label} for r in self._roots.values()]

    # ── Path checks ─────────────────────────────────────────────────

    def _root(self, name: str) -> FileRoot:
        root = self._roots.get(name)
        if root is None:
            raise NotFound(name)
        return root

    def _resolve(self, root: FileRoot, rel: str) -> Path:
        if "\x00" in rel or rel.startswith(("/", "\\")):
            raise NotAllowed(rel)
        parts = [p for p in PurePosixPath(rel).parts if p not in ("", ".")]
        if any(p == ".." or _is_hidden(p) for p in parts):
            raise NotAllowed(rel)
        target = root.path.joinpath(*parts)
        if not os.path.lexists(target):
            raise NotFound(rel)
        real = target.resolve()
        if real != root.path and not real.is_relative_to(root.path):
            raise NotAllowed(rel)
        return real

    def _rel(self, root: FileRoot, logical: Path) -> str:
        return logical.relative_to(root.path).as_posix() if logical != root.path else ""

    # ── Listing ─────────────────────────────────────────────────────

    def list_dir(self, root_name: str, rel: str = "") -> dict:
        root = self._root(root_name)
        real = self._resolve(root, rel)
        if not real.is_dir():
            raise NotFound(rel)
        logical = root.path.joinpath(*[p for p in PurePosixPath(rel).parts if p not in ("", ".")])
        entries = []
        with os.scandir(real) as it:
            for entry in it:
                item = self._entry(root, logical, entry)
                if item is not None:
                    entries.append(item)
        entries.sort(key=lambda e: (e["kind"] != "dir", -e["modified"], e["name"]))
        return {
            "root": root.name,
            "path": self._rel(root, logical),
            "entries": entries[:MAX_ENTRIES],
            "truncated": len(entries) > MAX_ENTRIES,
        }

    def _entry(self, root: FileRoot, logical_dir: Path, entry: os.DirEntry) -> dict | None:
        if _is_hidden(entry.name):
            return None
        try:
            real = Path(entry.path).resolve()
            if real != root.path and not real.is_relative_to(root.path):
                return None
            st = real.stat()
        except OSError:
            return None
        rel = self._rel(root, logical_dir / entry.name)
        if real.is_dir():
            return {
                "name": entry.name,
                "path": rel,
                "kind": "dir",
                "size": None,
                "modified": st.st_mtime,
                "preview": self._preview(real, rel),
            }
        if not real.is_file():
            return None
        return {
            "name": entry.name,
            "path": rel,
            "kind": kind_of(entry.name)[0],
            "size": st.st_size,
            "modified": st.st_mtime,
        }

    def _preview(self, real_dir: Path, rel_dir: str) -> str | None:
        """A cover image for a folder card: an image named like a cover."""
        try:
            with os.scandir(real_dir) as it:
                names = sorted(e.name for e in it if e.is_file() and not _is_hidden(e.name))
        except OSError:
            return None
        images = [n for n in names if kind_of(n)[0] == "image"]
        covers = [n for n in images if "cover" in n.lower()]
        if not covers:
            return None
        return f"{rel_dir}/{covers[0]}" if rel_dir else covers[0]

    # ── Files ───────────────────────────────────────────────────────

    def resolve_file(self, root_name: str, rel: str) -> tuple[Path, str]:
        root = self._root(root_name)
        real = self._resolve(root, rel)
        if not real.is_file() or _is_hidden(real.name):
            raise NotFound(rel)
        return real, kind_of(real.name)[1]

    def locate(self, path: str) -> dict[str, str]:
        """Map an absolute or `~/` path, as it appears in a chat, to a root."""
        if path == "~" or path.startswith("~/"):
            path = self._home + path[1:]
        if "\x00" in path or not path.startswith("/"):
            raise NotAllowed(path)
        logical = Path(os.path.normpath(path))
        # Try the path as written, then with symlinks resolved (like /var
        # and /private/var on macOS).
        for candidate in (logical, logical.resolve()):
            for root in self._roots.values():
                if candidate == root.path or candidate.is_relative_to(root.path):
                    rel = candidate.relative_to(root.path).as_posix() if candidate != root.path else ""
                    real = self._resolve(root, rel)
                    kind = "dir" if real.is_dir() else kind_of(real.name)[0]
                    return {"root": root.name, "path": rel, "kind": kind}
        raise NotFound(path)
