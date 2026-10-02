"""Sharing one folder with Claude from the bar.

The user picks the folder here rather than in Claude's own folder picker,
because with `allowedWorkspaceFolders` emptied that picker has nothing to offer
(docs/adr/0009). What these tests hold in place is the part a user would notice
going wrong: that nothing is served until they say so, that the folder's vault
joins the ones restore searches, that the count they were shown is the count
that gets served, and that the serving stops when the real values do.
"""
import json
import os
import socket
import sys
import threading
import time
import types
import urllib.request
from pathlib import Path

import pytest


# One fake accessibility layer for the whole suite. Installing a second, thinner
# one here left whichever module ran first in sys.modules and broke the other.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_desktop_overlay import _fake_uia          # noqa: E402
from test_broker import Peer                       # noqa: E402  (one MCP client, not two)


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


_started = []


@pytest.fixture(autouse=True)
def _stop_serving():
    """Every broker a test starts is stopped afterwards. One left running holds
    the port, and the next test's share fails for a reason that has nothing to do
    with what it is testing."""
    yield
    while _started:
        try:
            _started.pop().stop()
        except Exception:  # noqa: BLE001
            pass


def free_port() -> int:
    """A port nothing is on. Not 0: the helper reads `sharePort or DEFAULT_PORT`,
    so a zero means the default, and every test would fight over one port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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
    _started.append(a.sharing)
    # __init__ is bypassed, so anything a poll legitimately expects is set here.
    a.claude_cfg, a.claude_cfg_at, a.claude_said = None, 0.0, None
    a.forgot = False
    a.emitted = []
    a.emit = lambda **kw: a.emitted.append(kw)
    a.toasts = []
    a.toast = lambda msg, error=False: a.toasts.append((msg, error))
    a.set_tip = lambda *args: None
    return a


def config(tmp_path, **kw):
    cfg = {"serverUrl": "http://127.0.0.1:1", "token": "tok", "apiKey": "",
           "sessions": {}, "chats": {}, "sharing": True, "shareRoot": "",
           "shareNames": "mask", "shareAllowCode": False, "sharePort": free_port()}
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
    assert "names is masked" in said


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
    assert a.sharing.name == "other"
    assert a.sharing.workspace.root.name == "other"
    # The folder is swapped in under the running server rather than the server
    # being restarted, so the connection Claude Desktop already has survives a
    # change of folder. (The autouse fixture stops it afterwards.)
    assert a.sharing.httpd is first


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


def test_an_unattended_desk_drops_the_values_and_keeps_serving(helper, folder, tmp_path):
    """Reported on the first Windows run: locking the screen stopped the folder
    mid-task. Dropping the values is right -- held indefinitely they are a
    standing disclosure on an empty desk -- but it says nothing about the folder,
    which is masked by the server and not by anything held in this process."""
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    assert a.index.vault, "the folder's values were fetched"
    a.forget_vaults("the workstation was locked")
    assert not a.index.vault, "the values are gone"
    assert a.sharing.active, "and the folder is still being served"


def test_the_values_come_back_when_the_person_does(helper, folder, tmp_path, monkeypatch):
    """Otherwise the tokens on screen stay unreadable until something else
    happens to refresh them."""
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    a.forget_vaults("the workstation was locked")
    a.forgot = True
    monkeypatch.setattr(helper, "workstation_locked", lambda: False)
    monkeypatch.setattr(helper, "idle_seconds", lambda: 2)
    a.poll_idle()
    assert a.index.vault.get("TOK_PERSON_1") == "Nimal Perera"
    assert a.forgot is False, "and it does not keep re-fetching"


def test_signing_out_does_stop_the_folder(helper, folder, tmp_path, monkeypatch):
    """The broker serves with this sign-in. Once it is gone there is nothing to
    mask with, so the folder has to stop with it."""
    monkeypatch.setattr(helper, "webbrowser", types.SimpleNamespace(open=lambda u: None))
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    assert a.sharing.active
    a.cmd_sign_out()
    assert not a.sharing.active
    assert a.cfg["token"] == ""


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


# --------------------------------------------------------------- the file values
def test_a_value_claude_read_from_a_file_can_be_restored_on_screen(helper, folder, tmp_path):
    """The point of the whole thing, and what was broken on the first real run.

    The folder's vault is fetched when the folder is shared, and at that moment
    it is empty: every token in it is minted afterwards, as Claude reads files,
    by the broker calling the server directly. Nothing asked the helper, so its
    copy stayed empty and the overlay ignored the tokens on screen.
    """
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))

    # Nothing has been read yet, so there is nothing to refresh.
    assert a.poll_shared_vault() is None
    assert a.sharing.minted is False

    # Claude reads a file: the broker masks it and says so.
    a.server = types.SimpleNamespace(api=lambda path, method="GET", body=None: {
        "ok": True, "status": 200,
        "data": {"mappings": {"TOK_PERSON_1": "Nimal Perera",
                              "TOK_LK_NIC_9": "912345678V"}}})
    a.sharing.workspace.on_mint()
    a.poll_shared_vault()

    assert a.index.vault.get("TOK_LK_NIC_9") == "912345678V", \
        "a value read out of a file has to be restorable under the mouse"
    assert helper.SHARED["index"] is a.index, "and the overlay thread reads this one"


def test_one_fetch_covers_a_handful_of_files(helper, folder, tmp_path):
    """Claude reads a folder in handfuls; a fetch per file would be a fetch per
    token."""
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    calls = []
    a.server = types.SimpleNamespace(api=lambda path, method="GET", body=None: (
        calls.append(path), {"ok": True, "status": 200, "data": {"mappings": {}}})[1])
    for _ in range(5):
        a.sharing.workspace.on_mint()
    a.poll_shared_vault()
    a.poll_shared_vault()
    assert len(calls) == 1, calls


def test_nothing_is_fetched_when_no_folder_is_shared(helper, tmp_path):
    a = a_worker(helper, config(tmp_path))
    calls = []
    a.server = types.SimpleNamespace(api=lambda *args, **kw: (
        calls.append(args), {"ok": True, "status": 200, "data": {}})[1])
    a.sharing.minted = True
    a.poll_shared_vault()
    assert calls == []


# --------------------------------------------------------------- Claude asks
def test_the_broker_answers_before_anything_is_shared(helper, tmp_path):
    """It used to come up when a folder was shared, which made "nothing shared"
    look identical to "nothing running" -- and left Claude unable to ask for a
    folder, since asking goes through this."""
    a = a_worker(helper, config(tmp_path))
    assert a.sharing.listen() == ""
    assert a.sharing.listening and not a.sharing.active
    port = a.sharing.httpd.server_address[1]
    peer = Peer(port)
    names = {t["name"] for t in peer.rpc("tools/list")["result"]["tools"]}
    assert "request_folder" in names, "there is somebody to ask, so the tool is offered"
    said, is_error = peer.call("list_files")
    assert is_error and "request_folder" in said, said


def test_a_standalone_broker_does_not_offer_to_ask(helper, folder, tmp_path):
    """There would be nobody to ask: no bar, no picker."""
    desk = helper.broker_mod.Desk(workspace=object())
    assert "request_folder" not in {t["name"] for t in helper.broker_mod.wire_tools(desk)}


def test_claude_asking_opens_the_picker_and_waits_for_the_answer(helper, folder, tmp_path):
    """The tool call blocks because the model is waiting on what the person
    decided. Here the bar's side is played by this thread."""
    a = a_worker(helper, config(tmp_path))
    a.sharing.listen()
    answers = []

    def claude_asks():
        answers.append(a.sharing.ask_for_folder("to summarise the overdue loans"))

    asker = threading.Thread(target=claude_asks, daemon=True)
    asker.start()
    for _ in range(100):                     # the bar notices through SHARED
        if helper.SHARED.get("want_folder"):
            break
        time.sleep(0.01)
    assert helper.SHARED["want_folder"] == "to summarise the overdue loans"
    assert not answers, "and the tool call is still waiting"

    a.cmd_share_folder(str(folder))          # what the picker leads to
    asker.join(timeout=5)
    ok, said = answers[0]
    assert ok and "shared a folder" in said and "file(s)" in said
    assert helper.SHARED["want_folder"] is None


def test_a_refusal_reaches_claude_rather_than_hanging(helper, tmp_path):
    a = a_worker(helper, config(tmp_path))
    a.sharing.listen()
    answers = []
    asker = threading.Thread(target=lambda: answers.append(a.sharing.ask_for_folder("")),
                             daemon=True)
    asker.start()
    for _ in range(100):
        if helper.SHARED.get("want_folder"):
            break
        time.sleep(0.01)
    a.cmd_folder_declined("The person closed the folder picker without choosing one.")
    asker.join(timeout=5)
    ok, said = answers[0]
    assert ok is False and "closed the folder picker" in said


def test_being_declined_once_is_not_asked_again(helper, tmp_path):
    """Anthropic's own tool says it: if the person declines, ask in conversation
    rather than again."""
    a = a_worker(helper, config(tmp_path))
    a.sharing.listen()
    a.sharing.declined = True
    ok, said = a.sharing.ask_for_folder("please")
    assert ok is False
    assert "already declined" in said and "conversation" in said


def test_sharing_clears_the_refusal_so_a_later_task_may_ask(helper, folder, tmp_path):
    a = a_worker(helper, config(tmp_path))
    a.sharing.declined = True
    a.cmd_share_folder(str(folder))
    assert a.sharing.declined is False


def test_asking_while_a_folder_is_shared_says_so_instead(helper, folder, tmp_path):
    a = a_worker(helper, config(tmp_path))
    a.cmd_share_folder(str(folder))
    desk = a.sharing.desk
    said = helper.broker_mod.tool_request_folder(desk, {"reason": "more files"})
    assert "already shared" in said and "list_files" in said


def test_a_request_nobody_answers_gives_up_rather_than_hanging(helper, tmp_path, monkeypatch):
    """A tool call that never returns is worse than one that says nobody
    answered."""
    monkeypatch.setattr(helper, "ASK_FOLDER_S", 0.05)
    a = a_worker(helper, config(tmp_path))
    a.sharing.listen()
    ok, said = a.sharing.ask_for_folder("")
    assert ok is False
    assert "did not answer in time" in said
    assert helper.SHARED["want_folder"] is None, "and the bar stops being asked"


# --------------------------------------------------------------- the packaged shape
def test_the_helper_is_also_the_bridge(helper, tmp_path, monkeypatch, capsys):
    """A packaged build is one executable. Claude Desktop starts it with --stdio
    when the broker is registered as a stdio server, and there is no separate
    broker.py in Program Files to point at, so the same entry point answers to
    both jobs."""
    import io
    monkeypatch.setattr(sys, "argv", ["SafePIIHelper.exe", "--stdio", "--bridge",
                                      "--port", "9"])
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert helper.main() == 0
    reply = json.loads(out.getvalue().strip())
    assert [t["name"] for t in reply["result"]["tools"]][:1] == ["list_files"]


# --------------------------------------------------------------- watching Claude's config
def a_config(tmp_path, servers: dict) -> Path:
    p = tmp_path / "claude_desktop_config.json"
    p.write_text(json.dumps({"mcpServers": servers}), "utf-8")
    return p


def watching(helper, tmp_path, servers, monkeypatch, **kw):
    """A worker already looking at a config file we control."""
    a = a_worker(helper, config(tmp_path, **kw))
    path = a_config(tmp_path, servers)
    monkeypatch.setattr(helper, "claude_config_paths", lambda: [path])
    a.events_sent = []
    a.report_event = lambda kind, detail: a.events_sent.append((kind, detail))
    return a, path


def test_an_mcp_server_that_is_not_ours_is_reported(helper, tmp_path, monkeypatch):
    """The one hole left on a standard deployment: a user can add a filesystem
    server, read raw files, and SafePII would never see them. It cannot be
    prevented without a policy Anthropic scopes to third-party deployments, so it
    is made visible instead."""
    a, _ = watching(helper, tmp_path, {"safepii-files": {}, "filesystem": {}}, monkeypatch)
    a.poll_claude_config()
    said = [e for e in a.emitted if e.get("type") == "alert"]
    assert said and said[0]["level"] == "warn"
    assert "filesystem" in said[0]["msg"]
    assert "do not pass through SafePII" in said[0]["msg"]
    assert a.events_sent == [("unmanaged-mcp-server", f"{a.claude_cfg}: filesystem")]


def test_our_own_server_is_not_reported_as_foreign(helper, tmp_path, monkeypatch):
    a, _ = watching(helper, tmp_path, {"safepii-files": {}}, monkeypatch)
    a.poll_claude_config()
    assert not [e for e in a.emitted if e.get("type") == "alert"]
    assert a.events_sent == []


def test_safepii_missing_from_the_config_is_reported_too(helper, tmp_path, monkeypatch):
    """Why nothing works, said once, instead of the user wondering."""
    a, _ = watching(helper, tmp_path, {}, monkeypatch)
    a.sharing.listen()
    a.poll_claude_config()
    assert [k for k, _ in a.events_sent] == ["safepii-not-registered"]
    assert any("not registered with Claude Desktop" in e.get("msg", "") for e in a.emitted)


def test_it_is_said_once_not_on_every_tick(helper, tmp_path, monkeypatch):
    a, _ = watching(helper, tmp_path, {"filesystem": {}}, monkeypatch)
    for _ in range(5):
        a.poll_claude_config()
    assert len(a.events_sent) == 1


def test_a_change_is_noticed_and_said_again(helper, tmp_path, monkeypatch):
    a, path = watching(helper, tmp_path, {"safepii-files": {}}, monkeypatch)
    a.poll_claude_config()
    assert a.events_sent == []
    path.write_text(json.dumps({"mcpServers": {"safepii-files": {}, "sneaky": {}}}), "utf-8")
    os.utime(path, (1, 1))
    a.poll_claude_config()
    assert [k for k, _ in a.events_sent] == ["unmanaged-mcp-server"]


def test_an_unreadable_config_is_not_an_alarm(helper, tmp_path, monkeypatch):
    """Claude rewrites that file; catching it half-written must not cry wolf."""
    a, path = watching(helper, tmp_path, {}, monkeypatch)
    path.write_text("{ not json", "utf-8")
    a.poll_claude_config()
    assert a.events_sent == []
    assert not [e for e in a.emitted if e.get("type") == "alert"]


def test_an_administrator_can_switch_the_watch_off(helper, tmp_path, monkeypatch):
    a, _ = watching(helper, tmp_path, {"filesystem": {}}, monkeypatch, watchClaudeConfig=False)
    a.poll_claude_config()
    assert a.events_sent == []


def test_the_packaged_install_path_is_among_those_looked_at(helper, monkeypatch):
    """A real machine kept it under Packages\\...\\LocalCache, nowhere near
    %APPDATA%, which is how this was found at all."""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
    # Separators are the running platform's; what matters is the shape.
    looked = [str(p).replace("\\", "/") for p in helper.claude_config_paths()]
    assert any("Packages" in p and "LocalCache" in p for p in looked), looked
    assert any(p.endswith("Roaming/Claude/claude_desktop_config.json") for p in looked), looked
