"""The zotero-mcp bridge: the .xpi it builds, its HTTP client, zotero_find_full_text and the CLI.

The add-on's own JavaScript is tested in tests/zotero_bridge/bootstrap.test.mjs
(run from pytest by test_zotero_bridge_js.py). Here Zotero's server is faked
at the `requests` boundary, the way test_better_bibtex_client.py does it.
"""

import argparse
import json
import zipfile

import pytest
import requests
from conftest import DummyContext

from zotero_mcp import cli_standalone, server, zotero_bridge
from zotero_mcp.cli_standalone import _CMD_MAP, build_parser
from zotero_mcp.tools import zotero_app

_NO_JSON = object()


class _Resp:
    def __init__(self, status=200, payload=_NO_JSON, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if payload is _NO_JSON else json.dumps(payload)

    def json(self):
        if self._payload is _NO_JSON:
            raise ValueError("not JSON")
        return self._payload


PING = {"ok": True, "id": "bridge@zotero-mcp", "version": "1.0.0", "protocol": 1,
        "actions": ["findFullText"], "allowed": True}


class FakeZoteroServer:
    """Answers GET /zotero-mcp/ping and POST /zotero-mcp/action, and records what was sent."""

    def __init__(self, monkeypatch, *, ping=PING, ping_status=200, action=None, action_status=200,
                 get_error=None, post_error=None):
        self.gets, self.posts = [], []
        self.ping, self.ping_status = ping, ping_status
        self.action = action if action is not None else {"ok": True, "finished": True, "results": []}
        self.action_status = action_status
        self.get_error, self.post_error = get_error, post_error
        monkeypatch.setattr(zotero_bridge.requests, "get", self._get)
        monkeypatch.setattr(zotero_bridge.requests, "post", self._post)

    def _get(self, url, headers=None, timeout=None):
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        if self.get_error:
            raise self.get_error
        return _Resp(self.ping_status, self.ping) if self.ping is not None else _Resp(self.ping_status, text="No endpoint found\n")

    def _post(self, url, headers=None, data=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "body": json.loads(data), "timeout": timeout})
        if self.post_error:
            raise self.post_error
        return _Resp(self.action_status, self.action)


@pytest.fixture(autouse=True)
def _personal_library(monkeypatch):
    monkeypatch.delenv("ZOTERO_LOCAL_PORT", raising=False)
    monkeypatch.setattr(zotero_app._client, "get_active_group_id", lambda: 0)


class TestXpi:
    def test_contains_the_addon_with_a_manifest_zotero_accepts(self, tmp_path):
        xpi = zotero_bridge.build_xpi(tmp_path)
        assert xpi == tmp_path / "zotero-mcp-bridge.xpi"
        with zipfile.ZipFile(xpi) as zf:
            assert sorted(zf.namelist()) == ["bootstrap.js", "manifest.json"]
            manifest = json.loads(zf.read("manifest.json"))
            assert b"/zotero-mcp/action" in zf.read("bootstrap.js")
        zotero = manifest["applications"]["zotero"]
        assert zotero["id"] == zotero_bridge.BRIDGE_ID
        # Zotero 10 refuses an add-on without update_url.
        assert zotero["update_url"].endswith("/src/zotero_mcp/zotero_bridge/updates.json")
        assert zotero["strict_min_version"] == "7.0"
        assert manifest["version"] == zotero_bridge.addon_version()

    def test_same_addon_same_bytes(self, tmp_path):
        first = zotero_bridge.build_xpi(tmp_path / "a").read_bytes()
        second = zotero_bridge.build_xpi(tmp_path / "b").read_bytes()
        assert first == second

    def test_update_manifest_names_the_addon(self):
        updates = json.loads((zotero_bridge._HERE / "updates.json").read_text())
        assert zotero_bridge.BRIDGE_ID in updates["addons"]


class TestClient:
    def test_ping_uses_loopback_and_the_configured_port(self, monkeypatch):
        fake = FakeZoteroServer(monkeypatch)
        monkeypatch.setenv("ZOTERO_LOCAL_PORT", "24119")
        assert zotero_bridge.ping() == PING
        assert fake.gets[0]["url"] == "http://127.0.0.1:24119/zotero-mcp/ping"
        assert not fake.gets[0]["headers"]["User-Agent"].startswith("Mozilla/"), "Zotero drops browser requests"

    @pytest.mark.parametrize(("fake_kwargs", "kind", "text"), [
        ({"get_error": requests.exceptions.ConnectionError()}, "zotero_not_running", "Zotero is not running"),
        ({"get_error": requests.exceptions.ReadTimeout()}, "timeout", "did not answer"),
        ({"ping": None, "ping_status": 404}, "bridge_missing", "zotero-cli bridge"),
        ({"ping": {"ok": True, "id": "someone-else"}}, "bad_response", "Something other than"),
        ({"ping": None, "ping_status": 500}, "bad_response", "HTTP 500"),
    ])
    def test_ping_failures_say_what_is_wrong(self, monkeypatch, fake_kwargs, kind, text):
        FakeZoteroServer(monkeypatch, **fake_kwargs)
        with pytest.raises(zotero_bridge.BridgeError) as exc:
            zotero_bridge.ping()
        assert exc.value.kind == kind
        assert text in str(exc.value)

    def test_run_action_posts_the_request(self, monkeypatch):
        fake = FakeZoteroServer(monkeypatch)
        assert zotero_bridge.run_action("findFullText", ["AAAAAAAA"], group_id=42, wait_seconds=30)["ok"]
        post = fake.posts[0]
        assert post["url"] == "http://127.0.0.1:23119/zotero-mcp/action"
        assert post["headers"]["Content-Type"] == "application/json"
        assert post["body"] == {"action": "findFullText", "groupID": 42, "itemKeys": ["AAAAAAAA"], "waitSeconds": 30}
        assert post["timeout"] > 30, "the bridge answers once its wait is over, so the client waits a bit longer"

    def test_an_old_bridge_is_told_apart_from_a_missing_one(self, monkeypatch):
        fake = FakeZoteroServer(monkeypatch, ping={**PING, "version": "0.9.0", "actions": []})
        with pytest.raises(zotero_bridge.BridgeError) as exc:
            zotero_bridge.run_action("findFullText", ["AAAAAAAA"])
        assert exc.value.kind == "bridge_outdated"
        assert "0.9.0" in str(exc.value) and "zotero-cli bridge" in str(exc.value)
        assert fake.posts == []

    @pytest.mark.parametrize(("status", "kind"), [(400, "bad_request"), (403, "forbidden"), (404, "not_found"),
                                                  (500, "bad_response")])
    def test_refusals_pass_the_bridge_message_on(self, monkeypatch, status, kind):
        FakeZoteroServer(monkeypatch, action={"ok": False, "error": "Turn on Allow other applications"},
                         action_status=status)
        with pytest.raises(zotero_bridge.BridgeError) as exc:
            zotero_bridge.run_action("findFullText", ["AAAAAAAA"])
        assert exc.value.kind == kind
        assert str(exc.value) == "Turn on Allow other applications"

    def test_a_slow_answer_is_a_timeout(self, monkeypatch):
        FakeZoteroServer(monkeypatch, post_error=requests.exceptions.ReadTimeout())
        with pytest.raises(zotero_bridge.BridgeError) as exc:
            zotero_bridge.run_action("findFullText", ["AAAAAAAA"], wait_seconds=1)
        assert exc.value.kind == "timeout"
        assert "may still be running" in str(exc.value)


RESULTS = {
    "ok": True, "action": "findFullText", "finished": True,
    "results": [
        {"key": "AAAAAAAA", "title": "Open paper", "status": "found",
         "attachment": {"key": "PDF00001", "contentType": "application/pdf", "title": "Full Text PDF"}},
        {"key": "BBBBBBBB", "title": "Closed paper", "status": "not_found_online"},
        {"key": "CCCCCCCC", "title": "Has one", "status": "skipped", "reason": "it already has a PDF or EPUB"},
        {"key": "DDDDDDDD", "status": "not_found"},
    ],
}


class TestTool:
    def test_reports_each_item(self, monkeypatch):
        FakeZoteroServer(monkeypatch, action=RESULTS)
        monkeypatch.setattr(zotero_app._utils, "is_local_mode", lambda: True)
        out = server.find_full_text(item_keys=["AAAAAAAA", "BBBBBBBB", "CCCCCCCC", "DDDDDDDD"], ctx=DummyContext())
        assert out.startswith("Find Full Text in Zotero: 1 found, 1 no file found, 1 skipped, 1 not in this library.")
        assert "- `AAAAAAAA` Open paper: found a PDF, attached as `PDF00001`" in out
        assert "- `BBBBBBBB` Closed paper: Zotero found no file for it" in out
        assert "- `CCCCCCCC` Has one: skipped, it already has a PDF or EPUB" in out
        assert "- `DDDDDDDD`: no item with this key in the current library" in out
        assert "syncs" not in out

    def test_sends_the_active_library_and_clean_keys(self, monkeypatch):
        fake = FakeZoteroServer(monkeypatch)
        monkeypatch.setattr(zotero_app._client, "get_active_group_id", lambda: 4711)
        server.find_full_text(item_keys='["aaaaaaaa", "BBBBBBBB", "aaaaaaaa"]', timeout=999, ctx=DummyContext())
        body = fake.posts[0]["body"]
        assert body["groupID"] == 4711
        assert body["itemKeys"] == ["AAAAAAAA", "BBBBBBBB"]
        assert body["waitSeconds"] == 50, "capped under the MCP client's ~60 s limit"

    def test_comma_separated_keys(self, monkeypatch):
        fake = FakeZoteroServer(monkeypatch)
        server.find_full_text(item_keys="AAAAAAAA, BBBBBBBB", timeout=0, ctx=DummyContext())
        assert fake.posts[0]["body"]["itemKeys"] == ["AAAAAAAA", "BBBBBBBB"]
        assert fake.posts[0]["body"]["waitSeconds"] == 0

    @pytest.mark.parametrize(("keys", "text"), [
        ([], "at least one item key"),
        (["not-a-key"], "Not item keys"),
        ([f"K{i:07d}" for i in range(101)], "At most 100"),
    ])
    def test_bad_keys_are_refused_before_calling_zotero(self, monkeypatch, keys, text):
        fake = FakeZoteroServer(monkeypatch)
        out = server.find_full_text(item_keys=keys, ctx=DummyContext())
        assert out.startswith("Error:") and text in out
        assert fake.gets == [] and fake.posts == []

    def test_bridge_problems_are_errors(self, monkeypatch):
        FakeZoteroServer(monkeypatch, ping=None, ping_status=404)
        out = server.find_full_text(item_keys=["AAAAAAAA"], ctx=DummyContext())
        assert out.startswith("Error: Zotero is running, but the zotero-mcp bridge add-on is not installed")

    def test_unfinished_items_point_to_a_later_check(self, monkeypatch):
        FakeZoteroServer(monkeypatch, action={"ok": True, "finished": False, "results": [
            {"key": "AAAAAAAA", "title": "Slow", "status": "pending"},
            {"key": "BBBBBBBB", "title": "Queued", "status": "queued"},
        ]})
        out = server.find_full_text(item_keys=["AAAAAAAA", "BBBBBBBB"], ctx=DummyContext())
        assert "1 still searching, 1 queued" in out
        assert "still working on 2 item(s)" in out and "zotero_get_item_children" in out

    def test_web_mode_mentions_sync(self, monkeypatch):
        FakeZoteroServer(monkeypatch, action=RESULTS)
        monkeypatch.setattr(zotero_app._utils, "is_local_mode", lambda: False)
        out = server.find_full_text(item_keys=["AAAAAAAA"], ctx=DummyContext())
        assert "once Zotero syncs" in out

    def test_nothing_to_do(self):
        assert zotero_app.format_find_full_text({"results": []}).startswith("Find Full Text in Zotero: nothing to do.")


class TestCli:
    def test_parsing(self):
        parser = build_parser()
        assert {"bridge", "find-full-text"} <= set(_CMD_MAP)
        args = parser.parse_args(["find-full-text", "AAAAAAAA", "BBBBBBBB", "--no-wait"])
        assert args.item_keys == ["AAAAAAAA", "BBBBBBBB"] and args.no_wait is True
        assert parser.parse_args(["find-full-text", "AAAAAAAA"]).timeout == 120
        assert parser.parse_args(["bridge", "--path"]).path is True
        assert parser.parse_args(["--json", "bridge"]).json_out is True

    def _bridge(self, monkeypatch, tmp_path, **kw):
        monkeypatch.setattr(zotero_bridge, "DEFAULT_DIR", tmp_path)
        ns = argparse.Namespace(path=False, reveal=False, json_out=False, verbose=False)
        for k, v in kw.items():
            setattr(ns, k, v)
        cli_standalone.cmd_bridge(ns)

    def test_bridge_path(self, monkeypatch, tmp_path, capsys):
        self._bridge(monkeypatch, tmp_path, path=True)
        out = capsys.readouterr().out
        assert out == f"{tmp_path / 'zotero-mcp-bridge.xpi'}\n"
        assert zipfile.is_zipfile(out.strip())

    def test_bridge_says_whether_it_is_installed(self, monkeypatch, tmp_path, capsys):
        FakeZoteroServer(monkeypatch, ping=None, ping_status=404)
        self._bridge(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "Install Plugin From File" in out and "Not installed in the running Zotero yet." in out

        FakeZoteroServer(monkeypatch, ping={**PING, "allowed": False})
        self._bridge(monkeypatch, tmp_path, json_out=True)
        env = json.loads(capsys.readouterr().out)
        assert env["ok"] is True
        assert env["data"] == {"path": str(tmp_path / "zotero-mcp-bridge.xpi"), "version": "1.0.0", "installed": "1.0.0"}

    def _find(self, monkeypatch, keys, **kw):
        monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
        ns = argparse.Namespace(item_keys=keys, timeout=120, no_wait=False, json_out=False, verbose=False)
        for k, v in kw.items():
            setattr(ns, k, v)
        cli_standalone.cmd_find_full_text(ns)

    def test_find_full_text_json(self, monkeypatch, capsys):
        fake = FakeZoteroServer(monkeypatch, action=RESULTS)
        self._find(monkeypatch, ["AAAAAAAA,BBBBBBBB"], json_out=True)
        env = json.loads(capsys.readouterr().out)
        assert env["ok"] is True and env["command"] == "find-full-text"
        assert env["data"] == {"finished": True, "results": RESULTS["results"]}
        assert fake.posts[0]["body"]["waitSeconds"] == 120
        assert fake.posts[0]["body"]["itemKeys"] == ["AAAAAAAA", "BBBBBBBB"]

    def test_find_full_text_no_wait_and_markdown(self, monkeypatch, capsys):
        fake = FakeZoteroServer(monkeypatch, action=RESULTS)
        self._find(monkeypatch, ["AAAAAAAA"], no_wait=True)
        assert fake.posts[0]["body"]["waitSeconds"] == 0
        assert "found a PDF, attached as `PDF00001`" in capsys.readouterr().out

    def test_find_full_text_failure_exits_with_the_kind(self, monkeypatch, capsys):
        FakeZoteroServer(monkeypatch, get_error=requests.exceptions.ConnectionError())
        with pytest.raises(SystemExit) as exc:
            self._find(monkeypatch, ["AAAAAAAA"], json_out=True)
        assert exc.value.code == 1
        env = json.loads(capsys.readouterr().out)
        assert env["ok"] is False and env["error"]["code"] == "zotero_not_running"
