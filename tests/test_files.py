"""Read-only file browsing for the phone (ticket 19).

The server owns a list of named roots (``ANOTHER_BRIDGE_FILE_ROOTS``,
like ``outputs=~/agent_outputs``). The phone only ever sends a root name
and a path relative to it, never an absolute path, and nothing outside a
root, hidden, or key-like is listed or served.
"""

import os
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from auth import verify_phone_key
from routers import files as files_router
from services.file_roots import (
    FileBrowser,
    FileRootsConfigError,
    NotAllowed,
    NotFound,
    parse_roots,
)

PHONE = "p" * 40
H = {"X-Coder-Key": PHONE}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small agent_outputs look-alike inside a fake home."""
    home = tmp_path / "home"
    root = home / "agent_outputs"
    post = root / "another_systems" / "posts" / "2026-10-24" / "gpt_reel"
    post.mkdir(parents=True)
    (post / "gpt.mp4").write_bytes(b"\x00" * 5000)
    (post / "gpt_cover.png").write_bytes(b"\x89PNG" + b"\x00" * 100)
    (post / "post.txt").write_text("The T in ChatGPT.\n")
    older = root / "another_systems" / "posts" / "2026-10-20"
    older.mkdir(parents=True)
    os.utime(older, (1_000_000, 1_000_000))
    (root / "report.pdf").write_bytes(b"%PDF-1.4")
    (root / "deck.pptx").write_bytes(b"PK")
    (root / ".env").write_text("SECRET=1")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "x.txt").write_text("no")
    (root / "server.pem").write_text("-----BEGIN")
    outside = home / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    (root / "link_out").symlink_to(outside, target_is_directory=True)
    (root / "link_in.txt").symlink_to(post / "post.txt")
    return home


@pytest.fixture
def browser(tree: Path) -> FileBrowser:
    return FileBrowser(parse_roots("outputs=~/agent_outputs", home=tree), home=tree)


# ── Config ──────────────────────────────────────────────────────────


def test_parse_roots_expands_home_and_keeps_order(tree: Path) -> None:
    (tree / "b").mkdir()
    roots = parse_roots(" outputs = ~/agent_outputs , other=~/b ", home=tree)
    assert [r.name for r in roots] == ["outputs", "other"]
    assert roots[0].path == (tree / "agent_outputs").resolve()
    assert roots[0].label == "agent_outputs"


def test_parse_roots_empty_means_off(tree: Path) -> None:
    assert parse_roots("", home=tree) == []
    assert parse_roots("  ", home=tree) == []


@pytest.mark.parametrize(
    "spec",
    [
        "outputs",  # no path
        "Bad Name=~/agent_outputs",  # name not a slug
        "outputs=~/missing",  # folder does not exist
        "outputs=~/agent_outputs/report.pdf",  # not a folder
        "outputs=/etc",  # outside home
        "outputs=~/agent_outputs,outputs=~/elsewhere",  # duplicate name
        "home=~",  # the whole home folder is too wide
    ],
)
def test_parse_roots_rejects_bad_config(tree: Path, spec: str) -> None:
    with pytest.raises(FileRootsConfigError):
        parse_roots(spec, home=tree)


# ── Listing ─────────────────────────────────────────────────────────


def test_roots_never_expose_absolute_paths(browser: FileBrowser) -> None:
    assert browser.roots() == [{"name": "outputs", "label": "agent_outputs"}]


def test_list_root_hides_dotfiles_keys_and_links_out(browser: FileBrowser) -> None:
    listing = browser.list_dir("outputs", "")
    names = [e["name"] for e in listing["entries"]]
    assert ".env" not in names
    assert ".hidden" not in names
    assert "server.pem" not in names
    assert "link_out" not in names
    assert set(names) == {"another_systems", "report.pdf", "deck.pptx", "link_in.txt"}
    assert listing["root"] == "outputs"
    assert listing["path"] == ""


def test_folders_come_first_then_newest_first(browser: FileBrowser) -> None:
    listing = browser.list_dir("outputs", "another_systems/posts")
    assert [e["name"] for e in listing["entries"]] == ["2026-10-24", "2026-10-20"]
    first = listing["entries"][0]
    assert first["kind"] == "dir"
    assert first["path"] == "another_systems/posts/2026-10-24"


def test_entries_carry_kind_size_and_a_folder_preview(browser: FileBrowser) -> None:
    day = browser.list_dir("outputs", "another_systems/posts/2026-10-24")
    reel = day["entries"][0]
    assert reel["name"] == "gpt_reel"
    assert reel["preview"] == "another_systems/posts/2026-10-24/gpt_reel/gpt_cover.png"
    files = {e["name"]: e for e in browser.list_dir("outputs", reel["path"])["entries"]}
    assert files["gpt.mp4"]["kind"] == "video"
    assert files["gpt.mp4"]["size"] == 5000
    assert files["gpt_cover.png"]["kind"] == "image"
    assert files["post.txt"]["kind"] == "text"
    assert isinstance(files["post.txt"]["modified"], float)
    top = {e["name"]: e for e in browser.list_dir("outputs", "")["entries"]}
    assert top["report.pdf"]["kind"] == "pdf"
    assert top["deck.pptx"]["kind"] == "other"


@pytest.mark.parametrize(
    "path",
    ["../elsewhere", "/etc", "another_systems/../../elsewhere", ".hidden", "link_out", "a\x00b"],
)
def test_paths_that_leave_the_root_or_hide_are_refused(browser: FileBrowser, path: str) -> None:
    with pytest.raises((NotAllowed, NotFound)):
        browser.list_dir("outputs", path)


def test_unknown_root_and_missing_folder_are_not_found(browser: FileBrowser) -> None:
    with pytest.raises(NotFound):
        browser.list_dir("nope", "")
    with pytest.raises(NotFound):
        browser.list_dir("outputs", "missing")


# ── Files ───────────────────────────────────────────────────────────


def test_resolve_file_returns_type_for_allowed_files(browser: FileBrowser, tree: Path) -> None:
    path, media_type = browser.resolve_file("outputs", "another_systems/posts/2026-10-24/gpt_reel/gpt.mp4")
    assert path.name == "gpt.mp4"
    assert media_type == "video/mp4"
    _, text_type = browser.resolve_file("outputs", "another_systems/posts/2026-10-24/gpt_reel/post.txt")
    assert text_type == "text/plain; charset=utf-8"
    _, other = browser.resolve_file("outputs", "deck.pptx")
    assert other == "application/octet-stream"


def test_symlink_that_stays_inside_the_root_is_fine(browser: FileBrowser) -> None:
    path, _ = browser.resolve_file("outputs", "link_in.txt")
    assert path.name == "post.txt"


@pytest.mark.parametrize("path", [".env", "server.pem", "link_out/secret.txt", "../elsewhere/secret.txt"])
def test_hidden_key_like_and_outside_files_are_refused(browser: FileBrowser, path: str) -> None:
    with pytest.raises((NotAllowed, NotFound)):
        browser.resolve_file("outputs", path)


def test_a_folder_is_not_a_file(browser: FileBrowser) -> None:
    with pytest.raises(NotFound):
        browser.resolve_file("outputs", "another_systems")


# ── Locate (tappable paths in chat) ─────────────────────────────────


def test_locate_maps_absolute_and_home_paths_to_a_root(browser: FileBrowser, tree: Path) -> None:
    rel = "another_systems/posts/2026-10-24/gpt_reel/gpt.mp4"
    absolute = str(tree / "agent_outputs" / rel)
    assert browser.locate(absolute) == {"root": "outputs", "path": rel, "kind": "video"}
    assert browser.locate("~/agent_outputs/" + rel) == {"root": "outputs", "path": rel, "kind": "video"}


def test_locate_finds_folders_too(browser: FileBrowser) -> None:
    assert browser.locate("~/agent_outputs/another_systems/") == {
        "root": "outputs",
        "path": "another_systems",
        "kind": "dir",
    }


@pytest.mark.parametrize("path", ["~/elsewhere/secret.txt", "~/agent_outputs/.env", "relative/x.mp4", "~/agent_outputs/missing.mp4"])
def test_locate_refuses_anything_not_shared(browser: FileBrowser, path: str) -> None:
    with pytest.raises((NotAllowed, NotFound)):
        browser.locate(path)


# ── HTTP ────────────────────────────────────────────────────────────


@pytest.fixture
def client(browser: FileBrowser, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ANOTHER_BRIDGE_PHONE_KEY", PHONE)
    app = FastAPI()
    app.state.file_browser = browser
    app.include_router(files_router.router, dependencies=[Depends(verify_phone_key)])
    return TestClient(app)


def test_http_roots_and_list(client: TestClient) -> None:
    assert client.get("/files/roots", headers=H).json() == {"roots": [{"name": "outputs", "label": "agent_outputs"}]}
    resp = client.get("/files/list", params={"root": "outputs", "path": "another_systems/posts"}, headers=H)
    assert resp.status_code == 200
    assert [e["name"] for e in resp.json()["entries"]] == ["2026-10-24", "2026-10-20"]


def test_http_needs_the_phone_key(client: TestClient) -> None:
    assert client.get("/files/roots").status_code == 401


def test_http_errors_are_structured(client: TestClient) -> None:
    resp = client.get("/files/list", params={"root": "outputs", "path": "../elsewhere"}, headers=H)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "that path is not shared", "code": "not_shared"}
    resp = client.get("/files/list", params={"root": "outputs", "path": "missing"}, headers=H)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_http_raw_serves_the_file_inline(client: TestClient) -> None:
    rel = "another_systems/posts/2026-10-24/gpt_reel/post.txt"
    resp = client.get("/files/raw", params={"root": "outputs", "path": rel}, headers=H)
    assert resp.status_code == 200
    assert resp.text == "The T in ChatGPT.\n"
    assert resp.headers["content-type"] == "text/plain; charset=utf-8"
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_http_raw_supports_ranges_for_video(client: TestClient) -> None:
    rel = "another_systems/posts/2026-10-24/gpt_reel/gpt.mp4"
    resp = client.get("/files/raw", params={"root": "outputs", "path": rel}, headers={**H, "Range": "bytes=0-99"})
    assert resp.status_code == 206
    assert len(resp.content) == 100
    assert resp.headers["content-range"] == "bytes 0-99/5000"
    assert resp.headers["accept-ranges"] == "bytes"


def test_http_raw_refuses_hidden_files(client: TestClient) -> None:
    resp = client.get("/files/raw", params={"root": "outputs", "path": ".env"}, headers=H)
    assert resp.status_code == 403
    assert "SECRET" not in resp.text


def test_http_locate(client: TestClient) -> None:
    resp = client.get("/files/locate", params={"path": "~/agent_outputs/report.pdf"}, headers=H)
    assert resp.json() == {"root": "outputs", "path": "report.pdf", "kind": "pdf"}
    resp = client.get("/files/locate", params={"path": "~/elsewhere/secret.txt"}, headers=H)
    assert resp.status_code in (403, 404)


def test_http_with_no_roots_configured(monkeypatch: pytest.MonkeyPatch, tree: Path) -> None:
    monkeypatch.setenv("ANOTHER_BRIDGE_PHONE_KEY", PHONE)
    app = FastAPI()
    app.state.file_browser = FileBrowser([])
    app.include_router(files_router.router, dependencies=[Depends(verify_phone_key)])
    client = TestClient(app)
    assert client.get("/files/roots", headers=H).json() == {"roots": []}
    assert client.get("/files/list", params={"root": "outputs"}, headers=H).status_code == 404
