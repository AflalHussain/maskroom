"""The SafePII file broker: does it ever serve something unmasked?

The broker (`desktop/broker.py`) is an MCP server that hands Claude the files in
a folder with the personal data replaced. It is the only thing between a real
folder and the model, so the tests that matter are the refusals: a path outside
the folder, a type SafePII cannot check, a name that discloses before a byte is
read, a browser trying to drive a loopback server.

Both ends are real HTTP. A fake SafePII server stands in for the real one with a
deterministic masker, so a token in these tests means exactly one value and the
round trip can be checked rather than assumed.
"""
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))
broker = pytest.importorskip("broker")

# What the fake SafePII knows. Deterministic, so a token identifies its value.
VALUES = {
    "Nimal Perera": "TOK_PERSON_2615D96E",
    "Kamala Silva": "TOK_PERSON_7F31A0B2",
    "912345678V": "TOK_LK_NIC_5B20C1D4",
}
# A value only the tabular pipeline catches. The real engine behaves this way and
# it is the whole reason .csv is not served as prose: in a row like
# `Sunil Fernando,895647321X,...` a name recognizer has no context to work with,
# so the identifiers were masked and the person was not. The fake reproduces that
# asymmetry so the routing cannot silently regress.
TABULAR_ONLY = {"Sunil Fernando": "TOK_PERSON_C0FFEE01"}


class FakeSafePII(BaseHTTPRequestHandler):
    """/api/session, /api/mask, /api/unmask, and a counter for each."""
    protocol_version = "HTTP/1.1"
    calls: dict = {}
    runs: dict = {}
    fail_with: str = ""

    def log_message(self, fmt, *a):
        pass

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def reply(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def process(self):
        """The tabular pipeline: it masks by column, so it catches the name the
        prose path misses."""
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        marker = b'name="file"'
        start = raw.find(b"\r\n\r\n", raw.find(marker)) + 4
        end = raw.rfind(b"\r\n--")
        text = raw[start:end].decode("utf-8")
        for real, tok in {**VALUES, **TABULAR_ONLY}.items():
            text = text.replace(real, tok)
        run = f"run{len(type(self).runs) + 1}"
        type(self).runs[run] = text.encode()
        self.reply(200, {"run_id": run, "session_id": "s-test-1", "kind": "csv",
                         "findings": [], "downloads": {"output": "masked.csv", "vault": "vault.json"}})

    def do_GET(self):
        type(self).calls[self.path] = type(self).calls.get(self.path, 0) + 1
        run = self.path.rsplit("/", 2)
        blob = type(self).runs.get(run[-2]) if len(run) >= 2 else None
        if blob is None:
            self.reply(404, {"error": "no such run"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self):
        path = self.path
        type(self).calls[path] = type(self).calls.get(path, 0) + 1
        if type(self).fail_with:
            self.reply(503, {"error": type(self).fail_with})
            return
        if path == "/api/session":
            self.reply(200, {"session_id": "s-test-1"})
            return
        if path == "/api/process":
            self.process()
            return
        data = self.body()
        text = data.get("text", "")
        if path == "/api/mask":
            out, findings = text, []
            for real, tok in VALUES.items():
                if real in out:
                    out = out.replace(real, tok)
                    # The real /api/mask reports what it found and the token it
                    # minted; the broker needs both to mask a file name whose
                    # separators hid the value.
                    findings.append({"text": real, "token": tok, "entity_type": "PERSON"})
            self.reply(200, {"session_id": "s-test-1", "masked": out,
                             "changed": out != text, "findings": findings,
                             "vault_entries": len(VALUES)})
            return
        if path == "/api/unmask":
            out, restored, unresolved = text, 0, []
            for real, tok in VALUES.items():
                if tok in out:
                    out = out.replace(tok, real)
                    restored += 1
            for tok in {w for w in out.split() if w.startswith("TOK_")}:
                unresolved.append(tok)
            self.reply(200, {"session_id": "s-test-1", "text": out,
                             "restored": restored, "fuzzy": [], "unresolved": unresolved})
            return
        self.reply(404, {"error": "no such route"})


@pytest.fixture
def safepii():
    FakeSafePII.calls = {}
    FakeSafePII.runs = {}
    FakeSafePII.fail_with = ""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeSafePII)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def folder(tmp_path):
    """A folder shaped like a real one: text to serve, types to refuse, and a
    name that discloses on its own."""
    root = tmp_path / "work"
    (root / "kyc").mkdir(parents=True)
    (root / "notes.md").write_text("Call Nimal Perera on Monday about 912345678V.\nSecond line.\n")
    (root / "loans.csv").write_text("name,amount\nKamala Silva,450000\nNimal Perera,120000\n"
                                    "Sunil Fernando,90000\n")
    (root / "kyc" / "Nimal Perera - KYC.txt").write_text("NIC 912345678V verified.\n")
    (root / "app.py").write_text("SECRET = 'do not mask me'\n")
    (root / "book.xlsx").write_bytes(b"PK\x03\x04 not really a workbook")
    (root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n binary")
    (tmp_path / "outside.txt").write_text("Nimal Perera lives here, outside the root.\n")
    return root


class Peer:
    """An MCP client over real HTTP."""

    def __init__(self, port, token=""):
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.token = token
        self.n = 0

    def rpc(self, method, params=None, headers=None, expect=200):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        head = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        if self.token:
            head["Authorization"] = "Bearer " + self.token
        head.update(headers or {})
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(),
                                     method="POST", headers=head)
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                assert res.status == expect, res.status
                raw = res.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            assert e.code == expect, f"{e.code} not {expect}"
            return json.loads(e.read() or b"{}")

    def call(self, name, **arguments):
        out = self.rpc("tools/call", {"name": name, "arguments": arguments})
        result = out["result"]
        return result["content"][0]["text"], bool(result.get("isError"))


def start(safepii, folder, tmp_path, **kw):
    ws = broker.Workspace(folder, broker.Client(f"http://127.0.0.1:{safepii.server_address[1]}"),
                          staging=tmp_path / "out", **kw)
    httpd = broker.make_server(broker.Desk(ws), port=0)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    return ws, Peer(httpd.server_address[1]), httpd


@pytest.fixture
def served(safepii, folder, tmp_path):
    """A broker serving `folder`, plus a client. Names masked, which is the
    policy the name tests below are about; the default is handles."""
    ws, peer, httpd = start(safepii, folder, tmp_path, names="mask")
    yield ws, peer
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def default_served(safepii, folder, tmp_path):
    """A broker with nothing configured, to pin down what the defaults are."""
    ws, peer, httpd = start(safepii, folder, tmp_path)
    yield ws, peer
    httpd.shutdown()
    httpd.server_close()


# ------------------------------------------------------------------ the protocol
def test_a_client_can_start_a_session_and_see_the_tools(served):
    _ws, peer = served
    out = peer.rpc("initialize", {"protocolVersion": "2025-06-18",
                                  "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    r = out["result"]
    assert r["protocolVersion"] == "2025-06-18"
    assert r["serverInfo"]["name"] == broker.NAME
    assert "tools" in r["capabilities"]
    assert "TOK_" in r["instructions"], "the client is told what the tokens are up front"
    names = {t["name"] for t in peer.rpc("tools/list")["result"]["tools"]}
    assert names == {"list_files", "read_file", "search_files", "write_file"}


def test_a_newer_client_is_not_turned_away(served):
    """The subset used here has not changed between revisions, so a version we
    have never heard of is answered rather than refused."""
    _ws, peer = served
    out = peer.rpc("initialize", {"protocolVersion": "2093-01-01", "capabilities": {}})
    assert out["result"]["protocolVersion"] == "2093-01-01"


def test_a_notification_gets_no_answer_and_an_unknown_method_does(served):
    _ws, peer = served
    assert peer.rpc("notifications/initialized", {}, expect=202) is None
    out = peer.rpc("resources/list", {})
    assert out["error"]["code"] == -32601


def test_the_stream_channel_and_stray_verbs_are_refused(served):
    _ws, peer = served
    req = urllib.request.Request(peer.url, method="GET")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=20)
    assert e.value.code == 405


# ------------------------------------------------------------------ masking
def test_a_file_is_served_with_its_personal_data_replaced(served):
    _ws, peer = served
    text, is_error = peer.call("read_file", path="notes.md")
    assert not is_error
    assert "Nimal Perera" not in text and "912345678V" not in text
    assert "TOK_PERSON_2615D96E" in text and "TOK_LK_NIC_5B20C1D4" in text
    assert "1\t" in text and "2\t" in text, "lines are numbered for the masked text"


def test_a_table_is_masked_by_the_pipeline_that_understands_tables(served):
    """A .csv served as prose came back with the identifiers masked and the
    people not, because a name in a comma-separated row has no context around
    it. Found against a live server. Tables go through /api/process, which masks
    by column."""
    _ws, peer = served
    text, is_error = peer.call("read_file", path="loans.csv")
    assert not is_error, text
    assert "Sunil Fernando" not in text, "the prose path misses this one; the table path must not"
    assert "TOK_PERSON_C0FFEE01" in text
    assert FakeSafePII.calls.get("/api/process"), "a table must not go through /api/mask"


def test_the_tokens_are_explained_once_and_not_on_every_call(served):
    _ws, peer = served
    first, _ = peer.call("read_file", path="notes.md")
    second, _ = peer.call("read_file", path="loans.csv")
    assert "TOK_<TYPE>_<ID>" in first, "masked text has to be announced"
    assert "TOK_<TYPE>_<ID>" not in second, "but not repeated on every read"


def test_lines_are_counted_after_masking_not_before(served):
    """A token is longer than the value it replaced, so a slice taken from the
    real bytes points somewhere else."""
    _ws, peer = served
    text, _ = peer.call("read_file", path="notes.md", offset=2, limit=1)
    assert "Second line." in text
    assert "TOK_PERSON" not in text, "line 2 has no personal data in it"


# ------------------------------------------------------------------ names
def test_by_default_a_name_cannot_disclose_anything(default_served):
    """Handles, not masking, because masking a name is best-effort: measured
    against the real engine, `kyc/Nimal Perera - loan.csv` is not recognised at
    all. A handle cannot leak whatever the detector misses."""
    _ws, peer = default_served
    text, _ = peer.call("list_files")
    assert "Nimal Perera" not in text and "Perera" not in text
    listed = [ln.strip().split("  ")[0] for ln in text.splitlines() if ln.startswith("  ")]
    assert listed, text
    for name in listed:
        assert re.fullmatch(r"(?:d\d\d/)?f\d\d\d\.[a-z]+", name), f"{name} is not a handle"
    assert "handles" in text, "and the model is told why the names look like that"


def test_a_handle_is_stable_and_reads_back(default_served):
    _ws, peer = default_served
    first, _ = peer.call("list_files")
    again, _ = peer.call("list_files")
    handles = re.findall(r"(?:d\d\d/)?f\d\d\d\.txt", first)
    assert handles, first
    assert handles == re.findall(r"(?:d\d\d/)?f\d\d\d\.txt", again), "same file, same handle"
    text, is_error = peer.call("read_file", path=handles[0])
    assert not is_error, text
    assert "TOK_LK_NIC_5B20C1D4" in text


def test_the_handle_keeps_the_extension_so_the_type_is_known(default_served):
    _ws, peer = default_served
    text, _ = peer.call("list_files")
    assert ".csv" in text and ".md" in text and ".xlsx" in text


def test_masking_a_name_is_done_a_component_at_a_time(served):
    """A path separator glues the folder to the name and the detector, reading
    prose, sees one word. Masking each component separately is what fixes it --
    found against a live server, not guessed."""
    _ws, peer = served
    text, _ = peer.call("list_files")
    assert "Nimal Perera - KYC.txt" not in text, "the name disclosed before a byte was read"
    assert "TOK_PERSON_2615D96E - KYC.txt" in text


def test_a_name_whose_separators_hide_the_value_is_still_masked(served, safepii):
    """`Nimal_Perera_loan.csv` is not prose either. The component is probed again
    with its separators turned into spaces, and what that finds is put back."""
    ws, peer = served
    (ws.root / "Nimal_Perera_loan.csv").write_text("nothing here\n")
    text, _ = peer.call("list_files")
    assert "Nimal_Perera_loan.csv" not in text
    assert "TOK_PERSON_2615D96E_loan.csv" in text


def test_a_masked_name_can_be_read_back_to_us(served):
    """The model can only quote the name we gave it, so that name has to work."""
    _ws, peer = served
    peer.call("list_files")
    text, is_error = peer.call("read_file", path="kyc/TOK_PERSON_2615D96E - KYC.txt")
    assert not is_error, text
    assert "TOK_LK_NIC_5B20C1D4" in text


def test_real_names_are_an_explicit_choice(safepii, folder, tmp_path):
    _ws, peer, httpd = start(safepii, folder, tmp_path, names="real")
    try:
        text, _ = peer.call("list_files")
        assert "kyc/Nimal Perera - KYC.txt" in text
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_an_unknown_name_policy_is_refused_at_startup(safepii, folder, tmp_path):
    with pytest.raises(broker.BrokerError):
        broker.Workspace(folder, broker.Client("http://127.0.0.1:1"), names="whatever")


def test_a_file_is_masked_once_and_then_remembered(served):
    ws, peer = served
    peer.call("read_file", path="notes.md")
    after_first = FakeSafePII.calls["/api/mask"]
    peer.call("read_file", path="notes.md")
    assert FakeSafePII.calls["/api/mask"] == after_first, "a mount invites dozens of reads"
    (ws.root / "notes.md").write_text("Kamala Silva replaced the file.\n")
    os.utime(ws.root / "notes.md", (1, 1))
    text, _ = peer.call("read_file", path="notes.md")
    assert "TOK_PERSON_7F31A0B2" in text, "a changed file is masked again"


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("path,because", [
    ("app.py", "source code"),
    ("book.xlsx", "document pipeline"),
    ("photo.png", "cannot check"),
])
def test_what_safepii_cannot_mask_is_not_served(served, path, because):
    _ws, peer = served
    text, is_error = peer.call("read_file", path=path)
    assert is_error, text
    assert because in text
    assert "do not mask me" not in text


def test_a_refused_file_is_still_listed_so_its_absence_is_not_a_mystery(served):
    _ws, peer = served
    text, _ = peer.call("list_files")
    assert "app.py" in text and "NOT SERVED" in text


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "kyc/../../outside.txt"])
def test_a_path_out_of_the_folder_is_refused(served, path):
    _ws, peer = served
    text, is_error = peer.call("read_file", path=path)
    assert is_error, text
    assert "outside the folder" in text or "not a file" in text
    assert "outside the root" not in text, "and its contents certainly do not come back"


def test_a_symlink_out_of_the_folder_is_refused(served, tmp_path):
    ws, peer = served
    try:
        (ws.root / "escape.txt").symlink_to(tmp_path / "outside.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not make a symlink")
    text, is_error = peer.call("read_file", path="escape.txt")
    assert is_error, text
    assert "outside the folder" in text


def test_a_file_too_big_to_mask_is_refused_not_truncated(served):
    ws, peer = served
    (ws.root / "huge.txt").write_text("Nimal Perera\n" * 60_000)
    text, is_error = peer.call("read_file", path="huge.txt")
    assert is_error and "limit is" in text
    assert "Nimal Perera" not in text


def test_when_safepii_cannot_be_reached_nothing_is_served(served):
    """The guard fails closed, and so does this: no server, no file."""
    ws, peer = served
    FakeSafePII.fail_with = "pretend the engine is down"
    ws.cache.clear()
    text, is_error = peer.call("read_file", path="loans.csv")
    assert is_error, text
    assert "Kamala Silva" not in text
    assert "could not mask" in text or "could not start" in text


# ------------------------------------------------------------------ search
def test_searching_for_a_real_name_masks_the_query_and_finds_it(served):
    """The trick that makes search work at all: tokens are deterministic, so the
    name's token is what is actually searched for."""
    _ws, peer = served
    text, is_error = peer.call("search_files", query="Kamala Silva")
    assert not is_error, text
    assert "loans.csv:3" in text or "loans.csv:2" in text
    assert "TOK_PERSON_7F31A0B2" in text
    assert "searched for its token" in text, "and the model is told why"


def test_a_term_that_is_not_personal_data_is_searched_as_it_is(served):
    _ws, peer = served
    text, _ = peer.call("search_files", query="amount")
    assert "loans.csv:1" in text
    assert "searched for its token" not in text


def test_a_search_that_finds_nothing_says_why_it_might_not(served):
    _ws, peer = served
    text, _ = peer.call("search_files", query="Nimal")      # part of a value, not the value
    assert "No matches" in text
    assert "whole value" in text


# ------------------------------------------------------------------ writing back
def test_a_file_written_back_is_restored_and_lands_beside_the_folder(served, tmp_path):
    ws, peer = served
    text, is_error = peer.call("write_file", path="summary.md",
                               content="Approved: TOK_PERSON_7F31A0B2 on 450000.\n")
    assert not is_error, text
    out = tmp_path / "out" / "summary.md"
    assert out.read_text() == "Approved: Kamala Silva on 450000.\n"
    assert not (ws.root / "summary.md").exists(), "never into the folder it was given"
    assert "not the folder you were given" in text


def test_a_token_from_another_session_is_reported_not_guessed(served):
    _ws, peer = served
    text, _ = peer.call("write_file", path="x.md", content="Who is TOK_PERSON_DEADBEEF?\n")
    assert "TOK_PERSON_DEADBEEF" in text and "another session" in text


def test_a_write_cannot_climb_out_of_the_output_folder(served, tmp_path):
    _ws, peer = served
    text, is_error = peer.call("write_file", path="../../escaped.md", content="hello\n")
    assert is_error, text
    assert not (tmp_path / "escaped.md").exists()


# ------------------------------------------------------------------ the local surface
def test_a_web_page_cannot_drive_the_broker(served):
    """Any site's page can POST to 127.0.0.1, and this one reads files."""
    _ws, peer = served
    out = peer.rpc("tools/list", {}, headers={"Origin": "https://evil.example"}, expect=403)
    assert "cross-origin" in json.dumps(out)


def test_a_page_served_from_this_machine_is_still_allowed(served):
    _ws, peer = served
    out = peer.rpc("tools/list", {}, headers={"Origin": "http://localhost:3000"})
    assert out["result"]["tools"]


def test_a_bearer_token_is_enforced_when_one_is_set(safepii, folder, tmp_path):
    ws = broker.Workspace(folder, broker.Client(f"http://127.0.0.1:{safepii.server_address[1]}"),
                          staging=tmp_path / "out")
    httpd = broker.make_server(broker.Desk(ws), port=0, token="shared-secret")
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    try:
        port = httpd.server_address[1]
        Peer(port, token="shared-secret").rpc("tools/list", {})
        out = Peer(port).rpc("tools/list", {}, expect=401)
        assert "bearer" in json.dumps(out)
        out = Peer(port, token="wrong").rpc("tools/list", {}, expect=401)
        assert "bearer" in json.dumps(out)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_policy_snippet_is_what_an_administrator_needs(served):
    snippet = json.loads(broker.policy_snippet(47821, "abc"))
    entry = snippet["managedMcpServers"][0]
    assert entry["transport"] == "http", "a managed server may not speak stdio"
    assert entry["url"].startswith("http://127.0.0.1:"), "loopback is the accepted plain-HTTP host"
    assert entry["headers"]["Authorization"] == "Bearer abc"


# ------------------------------------------------------------------ the stdio bridge
def drive_stdio(monkeypatch, handle, messages):
    """Feed newline-delimited JSON-RPC in, collect what comes out."""
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(json.dumps(m) + "\n" for m in messages)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    broker.serve_stdio(handle)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_a_client_that_starts_the_process_gets_the_same_tools(served, monkeypatch):
    """Claude Desktop's own connector field wants an https address, because it is
    for a remote server. A process it starts and talks to over a pipe has no
    address, and so no certificate to argue about."""
    ws, _peer = served
    out = drive_stdio(monkeypatch, lambda m: broker.handle_rpc(broker.Desk(ws), m), [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "read_file", "arguments": {"path": "notes.md"}}}])
    assert out[0]["result"]["serverInfo"]["name"] == broker.NAME
    assert len(out[1]["result"]["tools"]) == 4
    assert "TOK_PERSON_2615D96E" in out[2]["result"]["content"][0]["text"]


def test_a_notification_produces_no_line_at_all(served, monkeypatch):
    """A line written for a notification would desynchronise the stream."""
    ws, _peer = served
    out = drive_stdio(monkeypatch, lambda m: broker.handle_rpc(broker.Desk(ws), m), [
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert len(out) == 1 and out[0]["id"] == 1


def test_rubbish_on_the_input_does_not_end_the_session(served, monkeypatch):
    import io
    ws, _peer = served
    monkeypatch.setattr("sys.stdin", io.StringIO(
        "not json\n\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    broker.serve_stdio(lambda m: broker.handle_rpc(broker.Desk(ws), m))
    assert [json.loads(l)["id"] for l in out.getvalue().splitlines() if l.strip()] == [9]


def test_the_bridge_relays_to_the_helpers_own_broker(served, monkeypatch):
    """One folder, served once, by the helper that holds the sign-in and the
    vault. The bridge only changes the transport Claude sees."""
    _ws, peer = served
    out = drive_stdio(monkeypatch, broker.forward_to(peer.url), [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "read_file", "arguments": {"path": "notes.md"}}}])
    assert "TOK_PERSON_2615D96E" in out[0]["result"]["content"][0]["text"]


def test_with_nothing_being_served_the_connector_still_looks_healthy(monkeypatch):
    """Claude Desktop starts this process when it starts, which is before anybody
    has shared anything. Failing there marks the connector broken for the whole
    session; answering normally and explaining at the point of use does not."""
    dead = "http://127.0.0.1:9/mcp"           # discard port: nothing ever listens
    monkeypatch.setattr(broker, "log", lambda m: None)
    out = drive_stdio(monkeypatch, broker.forward_to(dead, timeout=2), [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_files", "arguments": {}}}])
    assert out[0]["result"]["serverInfo"]["name"] == broker.NAME, "initialize must not fail"
    assert len(out[1]["result"]["tools"]) == 4, "the tools are still advertised"
    said = out[2]["result"]["content"][0]["text"]
    assert out[2]["result"]["isError"] is True
    assert "share one from the SafePII bar" in said, "and the model is told what to ask for"
    assert len(out) == 3, "the notification produced nothing"
