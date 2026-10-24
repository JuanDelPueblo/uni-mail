"""Tests for the MCP server: the tools end to end against a fake daemon, and the HTTP bearer middleware."""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uni_mail import mcp_server  # noqa: E402

try:
    from mcp import Client
except ImportError:
    Client = None

needs_mcp = unittest.skipUnless(Client, "the mcp package is not installed")

TOOLS = {
    "mail_status", "mail_list", "mail_search", "mail_show", "mail_folders",
    "mail_attachments", "mail_export_attachment", "mail_mark_read", "mail_mark_unread", "mail_archive", "mail_trash",
}
LONG_BODY = "x" * 100 + "END"


class FakeDaemon:
    """A uni-maild stand-in: one JSON line in, the canned JSON line for the op out. It records the requests."""

    def __init__(self, directory: str):
        self.path = os.path.join(directory, "mail.sock")
        self.requests: list[dict[str, Any]] = []
        self.responses: dict[str, Any] = {}
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                request = json.loads(self.rfile.readline())
                outer.requests.append(request)
                answer = outer.responses.get(request["op"], {"ok": False, "error": "unknown op"})
                if callable(answer):
                    answer = answer(request)
                self.wfile.write(json.dumps(answer).encode() + b"\n")

        self.server = socketserver.ThreadingUnixStreamServer(self.path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@needs_mcp
class McpTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="umcp")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.daemon = FakeDaemon(self.directory)
        self.addCleanup(self.daemon.close)
        self.daemon.responses = {
            "status": {"ok": True, "accounts": [{"name": "university", "status": "ok"}]},
            "list": {"ok": True, "count": 1, "messages": [{"id": "university.3f2a9c1d0b7e4a55", "subject": "Hello"}]},
            "show": {"ok": True, "id": "university.3f2a9c1d0b7e4a55", "body": LONG_BODY, "attachments": []},
            "folders": {"ok": True, "account": "university", "folders": ["INBOX"]},
            "attachments": {"ok": True, "id": "university.3f2a9c1d0b7e4a55", "attachments": []},
            "export_attachment": {"ok": True, "path": "/var/cache/a.pdf", "size": 3},
            "mark": {"ok": True, "results": {"university.3f2a9c1d0b7e4a55": {"ok": True}}},
            "archive": {"ok": True, "results": {"university.3f2a9c1d0b7e4a55": {"ok": True}}},
            "trash": {"ok": True, "results": {"university.3f2a9c1d0b7e4a55": {"ok": True}}},
        }

    async def call(self, name: str, arguments: dict[str, Any] | None = None, socket: str | None = None, **options: Any):
        server = mcp_server.build_server(self.daemon.path if socket is None else socket, **options)
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})

    def text(self, result: Any) -> str:
        return "".join(block.text for block in result.content)


class ToolsTest(McpTestCase):
    async def test_tool_list(self):
        async with Client(mcp_server.build_server(self.daemon.path)) as client:
            tools = (await client.list_tools()).tools
        self.assertEqual({tool.name for tool in tools}, TOOLS)
        self.assertEqual(len(tools), len(TOOLS))
        for tool in tools:
            self.assertTrue(tool.description, tool.name)
            self.assertIs(tool.annotations.open_world_hint, False)
            self.assertEqual(tool.annotations.read_only_hint, tool.name not in {
                "mail_export_attachment", "mail_mark_read", "mail_mark_unread", "mail_archive", "mail_trash"})

    async def test_no_forbidden_tools(self):
        async with Client(mcp_server.build_server(self.daemon.path)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
        for name in names:
            for word in ("auth", "setting", "event", "extract", "token", "run", "exec"):
                self.assertNotIn(word, name)

    async def test_status_drops_ok(self):
        result = await self.call("mail_status")
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {"accounts": [{"name": "university", "status": "ok"}]})
        self.assertEqual(self.daemon.requests, [{"op": "status"}])

    async def test_list_sends_filters(self):
        result = await self.call("mail_list", {"account": "university", "since": "2d", "until": "2026-09-01",
                                               "unread": True, "limit": 7})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["count"], 1)
        self.assertEqual(self.daemon.requests, [
            {"op": "list", "account": "university", "since": "2d", "until": "2026-09-01", "unread": True, "limit": 7}])

    async def test_list_defaults_send_only_the_limit(self):
        await self.call("mail_list")
        self.assertEqual(self.daemon.requests, [{"op": "list", "limit": 50}])

    async def test_search_sends_the_query(self):
        result = await self.call("mail_search", {"query": "invoice", "since": "12h", "limit": 3})
        self.assertFalse(result.is_error)
        self.assertEqual(self.daemon.requests, [{"op": "list", "query": "invoice", "since": "12h", "limit": 3}])

    async def test_search_needs_a_query(self):
        result = await self.call("mail_search", {"query": "  "})
        self.assertTrue(result.is_error)
        self.assertEqual(self.daemon.requests, [])

    async def test_limit_is_checked(self):
        for tool, limit in (("mail_list", 0), ("mail_list", 501), ("mail_search", -1)):
            result = await self.call(tool, {"query": "a", "limit": limit} if tool == "mail_search" else {"limit": limit})
            self.assertTrue(result.is_error, (tool, limit))
            self.assertIn("limit", self.text(result))
        self.assertEqual(self.daemon.requests, [])

    async def test_show_defaults(self):
        result = await self.call("mail_show", {"mail_id": "university.3f2a9c1d0b7e4a55"})
        self.assertFalse(result.is_error)
        self.assertEqual(self.daemon.requests, [{"op": "show", "id": "university.3f2a9c1d0b7e4a55", "full": True}])
        self.assertEqual(result.structured_content["body"], LONG_BODY)
        self.assertFalse(result.structured_content["truncated"])
        self.assertEqual(result.structured_content["body_chars"], len(LONG_BODY))

    async def test_show_truncates(self):
        result = await self.call("mail_show", {"mail_id": "a.b", "max_chars": 10, "full": False})
        data = result.structured_content
        self.assertEqual(data["body"], "x" * 10)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["body_chars"], len(LONG_BODY))
        self.assertEqual(self.daemon.requests[0]["full"], False)

    async def test_show_checks_max_chars(self):
        result = await self.call("mail_show", {"mail_id": "a.b", "max_chars": 0})
        self.assertTrue(result.is_error)
        self.assertIn("max_chars", self.text(result))

    async def test_folders_attachments_export(self):
        await self.call("mail_folders", {"account": "university"})
        await self.call("mail_attachments", {"mail_id": "a.b"})
        result = await self.call("mail_export_attachment", {"mail_id": "a.b", "index": 2})
        self.assertEqual(result.structured_content, {"path": "/var/cache/a.pdf", "size": 3})
        self.assertEqual(self.daemon.requests, [
            {"op": "folders", "account": "university"},
            {"op": "attachments", "id": "a.b"},
            {"op": "export_attachment", "id": "a.b", "index": 2},
        ])

    async def test_mark_and_archive(self):
        await self.call("mail_mark_read", {"mail_ids": ["a.b", " c.d "]})
        await self.call("mail_mark_unread", {"mail_ids": ["a.b"]})
        result = await self.call("mail_archive", {"mail_ids": ["a.b"]})
        self.assertFalse(result.is_error)
        result = await self.call("mail_trash", {"mail_ids": ["a.b"]})
        self.assertFalse(result.is_error)
        self.assertEqual(self.daemon.requests, [
            {"op": "mark", "ids": ["a.b", "c.d"], "read": True},
            {"op": "mark", "ids": ["a.b"], "read": False},
            {"op": "archive", "ids": ["a.b"]},
            {"op": "trash", "ids": ["a.b"]},
        ])

    async def test_daemon_error(self):
        self.daemon.responses["show"] = {"ok": False, "error": "message not found: nope.0"}
        result = await self.call("mail_show", {"mail_id": "nope.0"})
        self.assertTrue(result.is_error)
        self.assertIn("message not found: nope.0", self.text(result))

    async def test_daemon_not_running(self):
        result = await self.call("mail_status", socket=os.path.join(self.directory, "missing.sock"))
        self.assertTrue(result.is_error)
        self.assertIn("not running", self.text(result))

    async def test_partial_failure_lists_every_result(self):
        results = {"a.b": {"ok": True}, "c.d": {"ok": False, "error": "message not found"}}
        failure = {"ok": False, "error": "c.d: message not found", "results": results}
        self.daemon.responses["mark"] = failure
        self.daemon.responses["archive"] = failure
        for tool in ("mail_mark_read", "mail_mark_unread", "mail_archive"):
            result = await self.call(tool, {"mail_ids": ["a.b", "c.d"]})
            self.assertTrue(result.is_error, tool)
            text = self.text(result)
            self.assertIn("c.d: message not found", text)
            self.assertEqual(json.loads(text[text.index("{"):]), {"results": results})

    async def test_mail_ids_are_validated(self):
        for tool in ("mail_mark_read", "mail_mark_unread", "mail_archive"):
            for bad in ([], [""], ["a.b", "  "], ["a.b"] * 101, "a.b", [1]):
                result = await self.call(tool, {"mail_ids": bad})
                self.assertTrue(result.is_error, (tool, bad))
        self.assertEqual(self.daemon.requests, [])
        result = await self.call("mail_archive", {"mail_ids": ["a.b"] * 100})
        self.assertFalse(result.is_error)


class ScopeTest(McpTestCase):
    """--account and --read-only."""

    async def tools(self, **options: Any) -> set[str]:
        async with Client(mcp_server.build_server(self.daemon.path, **options)) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    async def test_read_only_leaves_out_the_writes(self):
        self.assertEqual(await self.tools(read_only=True), TOOLS - {
            "mail_export_attachment", "mail_mark_read", "mail_mark_unread", "mail_archive", "mail_trash"})
        self.assertEqual(await self.tools(accounts=("university",)), TOOLS)

    async def test_status_shows_only_the_accounts_served(self):
        self.daemon.responses["status"] = {"ok": True, "accounts": [{"name": "university"}, {"name": "gmail"}]}
        result = await self.call("mail_status", accounts=("gmail",))
        self.assertEqual(result.structured_content, {"accounts": [{"name": "gmail"}]})

    async def test_list_of_one_account_names_it(self):
        await self.call("mail_list", accounts=("university",))
        await self.call("mail_search", {"query": "exam"}, accounts=("university",))
        self.assertEqual(self.daemon.requests, [
            {"op": "list", "account": "university", "limit": 50},
            {"op": "list", "account": "university", "query": "exam", "limit": 50}])

    async def test_list_of_several_accounts_merges_them(self):
        lists = {
            "university": [{"id": "university.1", "date": "2026-10-03"}, {"id": "university.2", "date": "2026-10-01"}],
            "gmail": [{"id": "gmail.1", "date": "2026-10-02"}],
        }
        self.daemon.responses["list"] = lambda request: {
            "ok": True, "count": len(lists[request["account"]]), "messages": lists[request["account"]]}
        result = await self.call("mail_list", {"limit": 2}, accounts=("university", "gmail"))
        self.assertEqual(result.structured_content, {
            "count": 2, "messages": [{"id": "university.1", "date": "2026-10-03"}, {"id": "gmail.1", "date": "2026-10-02"}]})
        self.assertEqual([request["account"] for request in self.daemon.requests], ["university", "gmail"])

    async def test_other_accounts_are_refused(self):
        options = {"accounts": ("university",)}
        calls = [
            ("mail_list", {"account": "gmail"}),
            ("mail_search", {"query": "x", "account": "gmail"}),
            ("mail_folders", {"account": "gmail"}),
            ("mail_show", {"mail_id": "gmail.3f2a9c1d0b7e4a55"}),
            ("mail_attachments", {"mail_id": "gmail.3f2a9c1d0b7e4a55"}),
            ("mail_export_attachment", {"mail_id": "gmail.3f2a9c1d0b7e4a55", "index": 0}),
            ("mail_mark_read", {"mail_ids": ["university.3f2a9c1d0b7e4a55", "gmail.3f2a9c1d0b7e4a55"]}),
            ("mail_mark_unread", {"mail_ids": ["gmail.3f2a9c1d0b7e4a55"]}),
            ("mail_archive", {"mail_ids": ["gmail.3f2a9c1d0b7e4a55"]}),
            ("mail_trash", {"mail_ids": ["gmail.3f2a9c1d0b7e4a55"]}),
        ]
        for name, arguments in calls:
            result = await self.call(name, arguments, **options)
            self.assertTrue(result.is_error, name)
            self.assertIn("university", self.text(result), name)
        self.assertEqual(self.daemon.requests, [])
        result = await self.call("mail_archive", {"mail_ids": ["university.3f2a9c1d0b7e4a55"]}, **options)
        self.assertFalse(result.is_error)


@needs_mcp
class BearerAuthTest(unittest.TestCase):
    def client(self, app: Any = None, **kwargs: Any):
        try:
            from starlette.testclient import TestClient
        except (ImportError, RuntimeError) as error:
            self.skipTest(f"no starlette test client: {error}")

        async def ok(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

        return TestClient(mcp_server.BearerAuth(app or ok, "s3cret"), **kwargs)

    def test_refuses_without_token(self):
        response = self.client().get("/mcp")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_refuses_wrong_token(self):
        client = self.client()
        for header in ("Bearer nope", "Bearer s3cret2", "Bearer ", "Basic s3cret", "s3cret"):
            response = client.get("/mcp", headers={"Authorization": header})
            self.assertEqual(response.status_code, 401, header)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_passes_right_token(self):
        for scheme in ("Bearer", "bearer"):
            response = self.client().get("/mcp", headers={"Authorization": f"{scheme} s3cret"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "ok")

    def test_the_mcp_endpoint_behind_the_middleware(self):
        directory = tempfile.mkdtemp(prefix="umcp")
        self.addCleanup(shutil.rmtree, directory, True)
        app = mcp_server.build_server(os.path.join(directory, "none.sock")).streamable_http_app(host="127.0.0.1")
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        with self.client(app, base_url="http://127.0.0.1:8765") as client:
            self.assertEqual(client.post("/mcp", json=body, headers=headers).status_code, 401)
            response = client.post("/mcp", json=body, headers={**headers, "Authorization": "Bearer s3cret"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("uni-mail", response.text)


@needs_mcp
class HelpersTest(unittest.TestCase):
    def test_loopback(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.5"):
            self.assertTrue(mcp_server.is_loopback(host), host)
        for host in ("0.0.0.0", "192.168.1.5", "example.com", "::"):
            self.assertFalse(mcp_server.is_loopback(host), host)

    def test_token_sources(self):
        directory = tempfile.mkdtemp(prefix="umcp")
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "token"
        path.write_text("from-file\n")
        old = os.environ.pop("UNI_MAIL_MCP_TOKEN", None)
        self.addCleanup(lambda: os.environ.pop("UNI_MAIL_MCP_TOKEN", None) if old is None else os.environ.update(UNI_MAIL_MCP_TOKEN=old))
        self.assertEqual(mcp_server.read_token(str(path)), "from-file")
        self.assertEqual(mcp_server.read_token(""), "")
        os.environ["UNI_MAIL_MCP_TOKEN"] = " from-env "
        self.assertEqual(mcp_server.read_token(""), "from-env")

    def test_http_refuses_to_start(self):
        old = os.environ.pop("UNI_MAIL_MCP_TOKEN", None)
        self.addCleanup(lambda: os.environ.pop("UNI_MAIL_MCP_TOKEN", None) if old is None else os.environ.update(UNI_MAIL_MCP_TOKEN=old))
        self.assertEqual(mcp_server.main(["--transport", "streamable-http"]), 1)
        os.environ["UNI_MAIL_MCP_TOKEN"] = "t"
        self.assertEqual(mcp_server.main(["--transport", "streamable-http", "--host", "0.0.0.0"]), 1)


if __name__ == "__main__":
    unittest.main()
