"""Sharing one folder with Claude from the bar.

The user picks the folder here rather than in Claude's own folder picker,
because with `allowedWorkspaceFolders` emptied that picker has nothing to offer
(docs/adr/0009). What these tests hold in place is the part a user would notice
going wrong: that nothing is served until they say so, that the folder's vault
joins the ones restore searches, that the count they were shown is the count
that gets served, and that the serving stops when the real values do.
"""
import json
import sys
import threading
import types
import urllib.request
from pathlib import Path

import pytest


# One fake accessibility layer for the whole suite. Installing a second, thinner
# one here left whichever module ran first in sys.modules and broke the other.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_desktop_overlay import _fake_uia          # noqa: E402


@pytest.fixture
def helper(monkeypatch):
    sys.modules.setdefault("uiautomation", _fake_uia())
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "desktop"))
    mod = pytest.importorskip("helper")
    monkeypatch.setattr(mod, "log", lambda m: None)
    monkeypatch.setattr(mod, "save_config", lambda c: None)
    return mod


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "loans"
    root.mkdir()
    (root / "notes.md").write_text("Nimal Perera owes 450000.\n")
    (root / "register.csv").write_text("name,amount\nKamala Silva,120000\n")
    (root / "build.py").write_text("SECRET = 1\n")
    (root / "photo.png").write_bytes(b"\x89PNG binary")
    return root


def a_worker(helper, cfg, folder=None):
    """A worker with only what sharing needs, as the bar would have set it up."""
    a = helper.Automation.__new__(helper.Automation)
    a.cfg = cfg
    a.commands = __import__("queue").Queue()
    a.events = __import__("queue").Queue()
    a.vaults, a.token_owner = {}, {}
    a.index = helper.TokenIndex({})
    a.server = types.SimpleNamespace(
        api=lambda path, method="GET", body=None: (
            {"ok": True, "status": 200, "data": {"session_id": "s-folder-1"}}
            if path == "/api/session" else
            {"ok": True, "status": 200, "data": {"mappings": {"TOK_PERSON_1": "Nimal Perera"}}}))
    a.sharing = helper.Sharing(cfg, a.server)
    a.emitted = []
    a.emit = lambda **kw: a.emitted.append(kw)
    a.toasts = []
    a.toast = lambda msg, error=False: a.toasts.append((msg, error))
    a.set_tip = lambda *args: None
    return a


def config(tmp_path, **kw):
    cfg = {"serverUrl": "http://127.0.0.1:1", "token": "tok", "apiKey": "",
           "sessions": {}, "chats": {}, "sharing": True, "shareRoot": "",
           "shareNames": "handles", "shareAllowCode": False, "sharePort": 0}
    cfg.update(kw)
    return cfg


# --------------------------------------------------------------- before sharing
def test_nothing_is_served_until_the_user_picks_a_folder(helper, tmp_path):
    """Installing the helper must not expose anything on its own."""
    a = a_worker(helper, config(tmp_path))
    assert not a.sharing.active
    assert a.cfg["shareRoot"] == ""


def test_the_preview_is_what_the_user_is_shown_and_costs_no_server_call(helper, folder):
    """The count in the confirmation has to appear instantly and before anything
    leaves, so it is arithmetic over extensions, not masking."""
    p = helper.broker_mod.preview(folder)
    assert p["served"] == 2, p                      # the .md and the .csv
    assert p["refused"] == 2                        # the .py and the .png
    said = helper.broker_mod.describe(p)
    assert "2 file(s)" in said and "would not be served" in said
    assert "handles" in said


def test_source_files_are_counted_as_served_only_when_that_was_chosen(helper, folder):
    assert helper.broker_mod.preview(folder, allow_code=True)["served"] == 3


# --------------------------------------------------------------- sharing
def test_sharing_a_folder_serves_it_and_remembers_which(helper, folder, tmp_path):
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    try:
        assert a.sharing.active
        assert a.sharing.name == "loans"
        assert a.cfg["shareRoot"] == str(folder.resolve())
        said = [m for m, _err in a.toasts]
        assert any("Sharing loans" in m and "2 file(s)" in m for m in said), said
        event = [e for e in a.emitted if e.get("type") == "sharing"][-1]
        assert event["name"] == "loans" and event["served"] == 2
    finally:
        a.cmd_stop_sharing()


def test_the_folders_vault_joins_the_ones_restore_searches(helper, folder, tmp_path):
    """This is what makes a value read out of a file show up under the mouse in
    the chat. Without it the folder is masked and unreadable to its owner."""
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    try:
        assert "s-folder-1" in a.cfg["sessions"]
        assert a.cfg["sessions"]["s-folder-1"]["title"] == "Folder: loans"
        assert a.cfg["sessions"]["s-folder-1"]["folder"] is True
        assert "s-folder-1" in a.known_sessions()
        assert a.index.vault.get("TOK_PERSON_1") == "Nimal Perera"
    finally:
        a.cmd_stop_sharing()


def test_a_folders_session_is_not_the_session_the_chat_masks_into(helper, folder, tmp_path):
    """A folder's vault is not a conversation's. Binding it as the current
    session would send the next typed message into the folder's vault."""
    cfg = config(tmp_path, sessionId="s-chat-9")
    a = a_worker(helper, cfg)
    a.cmd_share_folder(str(folder))
    try:
        assert cfg["sessionId"] == "s-chat-9"
        assert cfg["chats"] == {}
    finally:
        a.cmd_stop_sharing()


def test_the_broker_answers_on_the_loopback_port_while_shared(helper, folder, tmp_path):
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    try:
        port = a.sharing.httpd.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp", method="POST",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as res:
            tools = {t["name"] for t in json.loads(res.read())["result"]["tools"]}
        assert "read_file" in tools
    finally:
        a.cmd_stop_sharing()


def test_sharing_a_second_folder_replaces_the_first(helper, folder, tmp_path):
    """One at a time: two folders means two vaults to explain and two sets of
    handles that look alike."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.md").write_text("nothing\n")
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    first = a.sharing.httpd
    a.cmd_share_folder(str(other))
    try:
        assert a.sharing.name == "other"
        assert a.sharing.httpd is not first
    finally:
        a.cmd_stop_sharing()


# --------------------------------------------------------------- stopping
def test_stopping_serves_nothing_and_forgets_the_folder(helper, folder, tmp_path):
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    port = a.sharing.httpd.server_address[1]
    a.cmd_stop_sharing()
    assert not a.sharing.active
    assert a.cfg["shareRoot"] == ""
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=3)


def test_dropping_the_real_values_stops_serving_the_folder(helper, folder, tmp_path):
    """Sign-out and the idle timer both drop the vaults. A folder still being
    served would keep answering with tokens this process can no longer turn back
    into values, which is worse than not serving it."""
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    assert a.sharing.active
    a.forget_vaults("signed out")
    assert not a.sharing.active


# --------------------------------------------------------------- refusals
def test_a_folder_that_is_not_there_is_refused_with_a_reason(helper, tmp_path):
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(tmp_path / "nope"))
    assert not a.sharing.active
    assert a.toasts and a.toasts[-1][1] is True
    assert "not a folder" in a.toasts[-1][0]


def test_an_administrator_can_forbid_sharing_entirely(helper, folder, tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "POLICY", {"sharing": False})
    a = a_worker(helper, config(tmp_path, sharing=False))
    a.cmd_share_folder(str(folder))
    assert not a.sharing.active
    assert "administrator" in a.toasts[-1][0]


def test_a_folder_shared_before_a_restart_comes_back(helper, folder, tmp_path):
    """The helper restarts on every update, and a folder quietly disappearing
    mid-task is worse than one that comes back saying so."""
    a = a_worker(helper, config(tmp_path, shareRoot=str(folder)))
    a.resume_share()
    assert a.commands.get_nowait() == ("share_folder", str(folder))


def test_a_folder_that_has_gone_is_not_resumed(helper, tmp_path):
    a = a_worker(helper, config(tmp_path, shareRoot=str(tmp_path / "deleted")))
    a.resume_share()
    assert a.commands.empty()
    assert a.cfg["shareRoot"] == ""
