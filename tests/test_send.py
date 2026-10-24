"""Tests for sending: the daemon action with a fake SMTP transport, the CLI, and the MCP tool with its protections.

No test opens a real SMTP connection: the daemon gets a fake SMTP factory.
"""

from __future__ import annotations

import dataclasses
import imaplib
import smtplib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1]))

from fake_imap import FakeIMAPServer, FakeTokenEndpoint, Mailbox  # noqa: E402
from test_mcp import McpTestCase, needs_mcp  # noqa: E402
from test_service import USER, sample_messages, wait_for  # noqa: E402
from uni_mail import auth, cli, config, mcp_server, send  # noqa: E402
from uni_mail.client import Client  # noqa: E402
from uni_mail.server import Service  # noqa: E402


class FakeSMTP:
    """Records what a real SMTP client would do. Nothing leaves the process."""

    instances: list["FakeSMTP"] = []
    refuse_auth = False
    refuse: dict = {}

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.calls: list[str] = []
        self.sent: list[tuple] = []
        self.credentials = ""
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        assert context is not None
        self.calls.append("starttls")

    def auth(self, mechanism, authobject):
        self.calls.append(f"auth {mechanism}")
        self.credentials = authobject()
        if FakeSMTP.refuse_auth:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.139 SmtpClientAuthentication is disabled for the Tenant")

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.sent.append((message, from_addr, list(to_addrs)))
        return {address: (550, b"no such user") for address in FakeSMTP.refuse if address in to_addrs}

    def quit(self):
        self.calls.append("quit")

    def close(self):
        pass


class NoSendScopeEndpoint(FakeTokenEndpoint):
    """A token endpoint whose sign-in was never given the SMTP.Send scope."""

    def respond(self, fields):
        if "scope" in fields:
            return 400, {"error": "invalid_grant", "error_description": "AADSTS65001: The user has not consented."}
        return super().respond(fields)


class SendServiceTest(unittest.TestCase):
    endpoint = FakeTokenEndpoint

    def setUp(self):
        FakeSMTP.instances, FakeSMTP.refuse_auth, FakeSMTP.refuse = [], False, {}
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.box = Mailbox(user=USER, token="good-access", messages=sample_messages())
        self.imap = FakeIMAPServer(self.box)
        self.tokens = self.endpoint(code="good-code", access_token="good-access")
        provider = dataclasses.replace(auth.PROVIDERS["microsoft"], token_endpoint=self.tokens.url)
        google = dataclasses.replace(auth.PROVIDERS["google"], token_endpoint=self.tokens.url)
        self.providers = mock.patch.dict(auth.PROVIDERS, {"microsoft": provider, "google": google})
        self.providers.start()
        self.service = None

    def tearDown(self):
        if self.service:
            self.service.stop()
            if not self.service.threads:
                self.service.store.close()
        self.providers.stop()
        self.imap.close()
        self.tokens.close()
        self.tmp.cleanup()

    def make(self, start: bool = False, provider: str = "microsoft", **account) -> Service:
        raw = {
            "state_dir": str(self.root / "state"), "socket": str(self.root / "mail.sock"),
            "export_dir": str(self.root / "exports"), "extract_root": None,
            "accounts": {"uni": {"provider": provider, "address": USER, "poll_seconds": 30, **account}},
        }
        cfg = config.parse(raw)
        auth.TokenStore(cfg.state_dir / "tokens").save("uni", {"refresh_token": "refresh-1"})
        port = self.imap.port
        self.service = Service(cfg, imap_factory=lambda host, _: imaplib.IMAP4("127.0.0.1", port, timeout=10),
                               smtp_factory=FakeSMTP)
        if start:
            self.service.start()
            wait_for(lambda: self.service.accounts["uni"].status == "idle", message="the first sync")
        return self.service

    def send(self, **request):
        return self.service.handle({"op": "send", "account": "uni", **request})

    def test_send_microsoft_uses_the_send_scope_and_xoauth2(self):
        self.make()
        response = self.send(to=["Tony <tony@example.org>"], cc="cc@example.org", bcc=["hidden@example.org"],
                             subject="Daily brief", body="Hello\nworld")
        self.assertTrue(response["ok"], response)
        self.assertEqual((response["to"], response["cc"], response["bcc"]), (["tony@example.org"], ["cc@example.org"], 1))
        self.assertTrue(response["message_id"].startswith("<"))
        (smtp,) = FakeSMTP.instances
        self.assertEqual((smtp.host, smtp.port), ("smtp.office365.com", 587))
        self.assertEqual(smtp.calls, ["ehlo", "starttls", "ehlo", "auth XOAUTH2", "quit"])
        self.assertEqual(smtp.credentials, f"user={USER}\x01auth=Bearer good-access\x01\x01")
        message, sender, envelope = smtp.sent[0]
        self.assertEqual(sender, USER)
        self.assertEqual(envelope, ["tony@example.org", "cc@example.org", "hidden@example.org"])
        self.assertEqual(message["Subject"], "Daily brief")
        self.assertEqual(message["Message-ID"], response["message_id"])
        self.assertIsNone(message["Bcc"])
        self.assertNotIn("hidden@example.org", message.as_string())
        scoped = [fields for fields in self.tokens.requests if "scope" in fields]
        self.assertEqual(len(scoped), 1)
        self.assertIn("SMTP.Send", scoped[0]["scope"])

    def test_send_google_needs_no_extra_scope(self):
        self.make(provider="google")
        response = self.send(to="tony@example.org", subject="Hi", body="text")
        self.assertTrue(response["ok"], response)
        (smtp,) = FakeSMTP.instances
        self.assertEqual(smtp.host, "smtp.gmail.com")
        self.assertFalse([fields for fields in self.tokens.requests if "scope" in fields])

    def test_html_makes_a_text_alternative(self):
        self.make()
        self.assertTrue(self.send(to="a@example.org", subject="H", body="<p>Hello <b>there</b></p>", html=True)["ok"])
        message = FakeSMTP.instances[0].sent[0][0]
        self.assertEqual(message.get_body(("plain",)).get_content().strip(), "Hello there")
        self.assertIn("<b>there</b>", message.get_body(("html",)).get_content())

    def test_reply_sets_headers_and_defaults(self):
        service = self.make(start=True)
        mail = service.handle({"op": "list"})["messages"][-1]  # Homework 4
        record = service.store.get(mail["id"])
        response = self.send(subject="", body="Done", reply_to=mail["id"])
        self.assertTrue(response["ok"], response)
        message = FakeSMTP.instances[0].sent[0][0]
        self.assertEqual(message["Subject"], "Re: Homework 4")
        self.assertEqual(message["In-Reply-To"], record["message_id"])
        self.assertEqual(message["References"], record["message_id"])
        self.assertEqual(response["to"], send.parse_addresses(record["sender"], "x"))

    def test_reply_to_unknown_message_is_refused(self):
        self.make()
        response = self.send(to="a@example.org", body="x", reply_to="uni.0000000000000000")
        self.assertFalse(response["ok"])
        self.assertFalse(FakeSMTP.instances)

    def test_bad_input_sends_nothing(self):
        self.make()
        cases = [
            {"to": ["a@example.org"], "subject": "x\r\nBcc: evil@example.org", "body": "b"},
            {"to": ["a@example.org, evil@example"], "subject": "s", "body": "b"},
            {"to": ["not an address"], "subject": "s", "body": "b"},
            {"to": [], "subject": "s", "body": "b"},
            {"to": ["a@example.org"], "subject": "", "body": "b"},
            {"to": ["a@example.org"], "subject": "s", "body": "  "},
            {"to": ["a@example.org"], "subject": "s" * 1000, "body": "b"},
            {"to": [f"u{i}@example.org" for i in range(51)], "subject": "s", "body": "b"},
            {"to": ["é@example.org"], "subject": "s", "body": "b"},
        ]
        for case in cases:
            with self.subTest(case=str(case)[:60]):
                self.assertFalse(self.send(**case)["ok"])
        self.assertFalse(FakeSMTP.instances)

    def test_missing_scope_says_how_to_sign_in_again(self):
        self.tokens.close()
        self.endpoint = NoSendScopeEndpoint
        self.tokens = NoSendScopeEndpoint(code="good-code", access_token="good-access")
        self.providers.stop()
        provider = dataclasses.replace(auth.PROVIDERS["microsoft"], token_endpoint=self.tokens.url)
        self.providers = mock.patch.dict(auth.PROVIDERS, {"microsoft": provider})
        self.providers.start()
        self.make()
        response = self.send(to="a@example.org", subject="s", body="b")
        self.assertFalse(response["ok"])
        self.assertIn("uni-mail auth login uni --send", response["error"])
        self.assertFalse(FakeSMTP.instances)
        # The stored sign-in is untouched.
        self.assertEqual(self.service.tokens.load("uni")["refresh_token"], "refresh-1")

    def test_smtp_auth_refused_explains(self):
        self.make()
        FakeSMTP.refuse_auth = True
        response = self.send(to="a@example.org", subject="s", body="b")
        self.assertFalse(response["ok"])
        self.assertIn("SMTP AUTH disabled", response["error"])
        self.assertNotIn("good-access", response["error"])
        self.assertEqual(FakeSMTP.instances[0].calls[-1], "quit")

    def test_partly_refused_recipients_are_reported(self):
        self.make()
        FakeSMTP.refuse = {"bad@example.org": 1}
        response = self.send(to=["ok@example.org", "bad@example.org"], subject="s", body="b")
        self.assertTrue(response["ok"])
        self.assertEqual(list(response["refused"]), ["bad@example.org"])

    def test_password_accounts_cannot_send(self):
        password = self.root / "pw"
        password.write_text("secret")
        self.make(provider="google", auth="password", password_file=str(password))
        response = self.send(to="a@example.org", subject="s", body="b")
        self.assertFalse(response["ok"])
        self.assertIn("cannot send", response["error"])

    def test_unknown_account(self):
        self.make()
        self.assertFalse(self.service.handle({"op": "send", "account": "nope", "to": "a@example.org", "subject": "s", "body": "b"})["ok"])

    def test_send_check_signs_in_and_sends_nothing(self):
        self.make()
        response = self.service.handle({"op": "send_check", "account": "uni"})
        self.assertTrue(response["ok"], response)
        (smtp,) = FakeSMTP.instances
        self.assertFalse(smtp.sent)
        self.assertEqual(smtp.calls[-2:], ["auth XOAUTH2", "quit"])

    def test_auth_begin_send_adds_the_scope_and_keeps_the_old_one(self):
        self.make()
        from urllib.parse import parse_qs, urlsplit

        plain = parse_qs(urlsplit(self.service.handle({"op": "auth_begin", "account": "uni"})["url"]).query)["scope"][0]
        extra = parse_qs(urlsplit(self.service.handle({"op": "auth_begin", "account": "uni", "send": True})["url"]).query)["scope"][0]
        self.assertNotIn("SMTP.Send", plain)
        self.assertIn("SMTP.Send", extra)
        for scope in plain.split():
            self.assertIn(scope, extra.split())

    def test_auth_begin_send_is_not_needed_for_google(self):
        self.make(provider="google")
        response = self.service.handle({"op": "auth_begin", "account": "uni", "send": True})
        self.assertFalse(response["ok"])
        self.assertIn("no extra consent", response["error"])

    def test_the_log_has_the_recipients_and_never_the_body(self):
        self.make()
        with self.assertLogs("uni_mail", "INFO") as logs:
            self.assertTrue(self.send(to="a@example.org", subject="Brief", body="SECRET-BODY-TEXT")["ok"])
        text = "\n".join(logs.output)
        self.assertIn("a@example.org", text)
        self.assertIn("Brief", text)
        self.assertIn("account=uni", text)
        self.assertNotIn("SECRET-BODY-TEXT", text)
        self.assertNotIn("good-access", text)


class CliTest(unittest.TestCase):
    def test_parser_reads_the_send_command(self):
        args = cli.parser().parse_args(["send", "--account", "gmail", "--to", "a@x.org", "--to", "b@x.org", "--cc", "c@x.org",
                                        "--subject", "S", "--body", "B", "--html", "--reply-to-message", "gmail.1"])
        self.assertEqual((args.to, args.cc, args.html, args.reply_to_message), (["a@x.org", "b@x.org"], ["c@x.org"], True, "gmail.1"))

    def test_body_and_account_are_required(self):
        for argv in (["send", "--account", "g", "--to", "a@x.org"], ["send", "--to", "a@x.org", "--body", "b"]):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                cli.parser().parse_args(argv)

    def test_send_goes_through_the_client(self):
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory, "body.txt")
            body.write_text("From a file")
            args = cli.parser().parse_args(["--config", "/nonexistent.json", "--socket", "/nonexistent.sock", "send", "--account",
                                            "gmail", "--to", "a@x.org", "--subject", "S", "--body-file", str(body)])
            with mock.patch.object(Client, "send", return_value={"ok": True}) as sent:
                cli.run(args)
        sent.assert_called_once_with("gmail", ["a@x.org"], "S", "From a file", [], [], False, "")

    def test_auth_login_send_flag(self):
        args = cli.parser().parse_args(["auth", "login", "outlook", "--send"])
        self.assertTrue(args.send)
        client = mock.Mock()
        client.auth_begin.return_value = {"url": "u", "redirect_uri": "r"}
        cli.login(client, "outlook", "https://localhost/?code=x", True)
        client.auth_begin.assert_called_once_with("outlook", True)


class PolicyTest(unittest.TestCase):
    def test_empty_allowlist_allows_nobody(self):
        with self.assertRaisesRegex(ValueError, "allowlist is empty"):
            mcp_server.SendPolicy().recipients(["tony@example.org"], None)

    def test_allowlist_matching(self):
        policy = mcp_server.SendPolicy(("Tony@Example.org", "@uni.edu"))
        self.assertEqual(policy.recipients("tony@example.org", ["x@UNI.edu"]), (["tony@example.org"], ["x@UNI.edu"]))
        for bad in ("evil@example.org", "tony@example.org.evil.com", "x@sub.uni.edu", "tony@example.org, evil@example.org"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                policy.recipients(bad, None)
        with self.assertRaisesRegex(ValueError, "evil@example.org"):
            policy.recipients(["tony@example.org"], ["evil@example.org"])

    def test_display_names_cannot_smuggle_an_address(self):
        policy = mcp_server.SendPolicy(("tony@example.org",))
        with self.assertRaises(ValueError):
            policy.recipients('"tony@example.org" <evil@example.org>', None)

    def test_bad_allowlist_entries(self):
        for entry in ("tony", "@", "a b@example.org"):
            with self.assertRaises(ValueError):
                mcp_server.SendPolicy((entry,))

    def test_fixed_account(self):
        policy = mcp_server.SendPolicy(account="gmail")
        self.assertEqual(policy.sender(""), "gmail")
        self.assertEqual(policy.sender("gmail"), "gmail")
        with self.assertRaises(ValueError):
            policy.sender("outlook")
        with self.assertRaises(ValueError):
            mcp_server.SendPolicy().sender("")

    def test_rate_limit_window(self):
        now = [0.0]
        policy = mcp_server.SendPolicy(max_per_hour=2, clock=lambda: now[0])
        policy.reserve()
        policy.reserve()
        with self.assertRaisesRegex(ValueError, "limit reached"):
            policy.reserve()
        now[0] = 3601
        policy.reserve()

    def test_size_limits(self):
        policy = mcp_server.SendPolicy(max_body=10, max_subject=5)
        policy.check_text("short", "0123456789")
        for subject, body in (("toolong", "b"), ("s", "x" * 11), ("a\nb", "b"), ("", "b"), ("s", " ")):
            with self.subTest(subject=subject), self.assertRaises(ValueError):
                policy.check_text(subject, body)


@needs_mcp
class McpSendTest(McpTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.daemon.responses["send"] = lambda request: {
            "ok": True, "account": request["account"], "message_id": "<1@x>", "to": request["to"], "cc": request.get("cc", []),
            "bcc": 0, "subject": request["subject"]}

    def server(self, **policy):
        return mcp_server.build_server(self.daemon.path, send=mcp_server.SendPolicy(**policy))

    async def send(self, arguments, **policy):
        from mcp import Client as McpClient

        async with McpClient(self.server(**policy)) as client:
            return await client.call_tool("mail_send", arguments)

    def sent_requests(self):
        return [request for request in self.daemon.requests if request["op"] == "send"]

    async def test_off_by_default(self):
        from mcp import Client as McpClient

        async with McpClient(mcp_server.build_server(self.daemon.path)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
        self.assertNotIn("mail_send", names)
        self.assertEqual(names, {tool.name for tool in await self.tools_of(self.server())} - {"mail_send", "mail_forward"})

    async def tools_of(self, server):
        from mcp import Client as McpClient

        async with McpClient(server) as client:
            return (await client.list_tools()).tools

    async def test_enabled_adds_only_mail_send(self):
        tools = {tool.name: tool for tool in await self.tools_of(self.server())}
        self.assertIn("mail_send", tools)
        self.assertFalse(tools["mail_send"].annotations.read_only_hint)
        self.assertNotIn("bcc", tools["mail_send"].input_schema["properties"])
        self.assertNotIn("html", tools["mail_send"].input_schema["properties"])

    async def test_empty_allowlist_refuses_everything(self):
        result = await self.send({"to": ["tony@example.org"], "subject": "s", "body": "b", "account": "gmail"})
        self.assertTrue(result.is_error)
        self.assertIn("allowlist is empty", self.text(result))
        self.assertFalse(self.sent_requests())

    async def test_allowed_send_reaches_the_daemon(self):
        result = await self.send({"to": ["tony@example.org"], "subject": " Brief ", "body": "text"}, allow=("tony@example.org",),
                                 account="gmail")
        self.assertFalse(result.is_error, self.text(result))
        (request,) = self.sent_requests()
        self.assertEqual((request["account"], request["to"], request["subject"], request["body"]),
                         ("gmail", ["tony@example.org"], "Brief", "text"))
        self.assertNotIn("bcc", request)
        self.assertNotIn("html", request)

    async def test_cc_outside_the_allowlist_is_refused(self):
        result = await self.send({"to": ["tony@example.org"], "cc": ["evil@example.org"], "subject": "s", "body": "b",
                                  "account": "gmail"}, allow=("tony@example.org",))
        self.assertTrue(result.is_error)
        self.assertIn("evil@example.org", self.text(result))
        self.assertFalse(self.sent_requests())

    async def test_fixed_account_cannot_be_overridden(self):
        result = await self.send({"to": ["tony@example.org"], "subject": "s", "body": "b", "account": "university"},
                                 allow=("tony@example.org",), account="gmail")
        self.assertTrue(result.is_error)
        self.assertFalse(self.sent_requests())

    async def test_rate_limit(self):
        policy = mcp_server.SendPolicy(("tony@example.org",), max_per_hour=2)
        from mcp import Client as McpClient

        async with McpClient(mcp_server.build_server(self.daemon.path, send=policy)) as client:
            results = [await client.call_tool("mail_send", {"to": ["tony@example.org"], "subject": "s", "body": "b",
                                                            "account": "gmail"}) for _ in range(3)]
        self.assertEqual([result.is_error for result in results], [False, False, True])
        self.assertIn("limit reached", self.text(results[2]))
        self.assertEqual(len(self.sent_requests()), 2)

    async def test_size_limits(self):
        result = await self.send({"to": ["tony@example.org"], "subject": "s", "body": "x" * 50, "account": "gmail"},
                                 allow=("tony@example.org",), max_body=10)
        self.assertTrue(result.is_error)
        self.assertFalse(self.sent_requests())

    async def test_daemon_refusal_is_a_tool_error(self):
        self.daemon.responses["send"] = {"ok": False, "error": "this sign-in cannot send mail yet"}
        result = await self.send({"to": ["tony@example.org"], "subject": "s", "body": "b", "account": "gmail"},
                                 allow=("tony@example.org",))
        self.assertTrue(result.is_error)
        self.assertIn("cannot send", self.text(result))

    async def test_sends_are_logged_without_the_body(self):
        with self.assertLogs("uni_mail.send", "INFO") as logs:
            await self.send({"to": ["tony@example.org"], "subject": "Brief", "body": "SECRET-BODY", "account": "gmail"},
                            allow=("tony@example.org",))
            await self.send({"to": ["evil@example.org"], "subject": "Brief", "body": "SECRET-BODY", "account": "gmail"},
                            allow=("tony@example.org",))
        text = "\n".join(logs.output)
        self.assertIn("<1@x>", text)
        self.assertIn("evil@example.org", text)
        self.assertNotIn("SECRET-BODY", text)


class McpMainTest(unittest.TestCase):
    def run_main(self, argv, env=None):
        captured = {}

        def fake_build(socket="", log_level="WARNING", send=None, accounts=(), read_only=False):
            captured.update(send=send, accounts=accounts, read_only=read_only)
            raise SystemExit(0)

        with (mock.patch.object(mcp_server, "build_server", fake_build), mock.patch("sys.stderr"),
              mock.patch.dict("os.environ", env or {}, clear=False)):
            try:
                code = mcp_server.main(["--socket", "/x.sock", *argv])
            except SystemExit as stop:
                code = stop.code
        return code, captured

    def test_off_by_default(self):
        code, captured = self.run_main([])
        self.assertIsNone(captured["send"])

    def test_account_and_read_only_reach_the_server(self):
        _, captured = self.run_main(["--account", "university,gmail", "--account", "outlook", "--read-only"])
        self.assertEqual(captured["accounts"], ("university", "gmail", "outlook"))
        self.assertIs(captured["read_only"], True)
        _, captured = self.run_main([])
        self.assertEqual((captured["accounts"], captured["read_only"]), ((), False))

    def test_options_without_enable_do_not_turn_it_on(self):
        _, captured = self.run_main(["--send-allow", "tony@example.org"])
        self.assertIsNone(captured["send"])

    def test_enable_with_no_allowlist_allows_nobody(self):
        _, captured = self.run_main(["--enable-send"], {"UNI_MAIL_MCP_SEND_ALLOW": ""})
        self.assertEqual(captured["send"].allow, ())

    def test_flags_and_environment(self):
        _, captured = self.run_main(["--enable-send", "--send-allow", "a@x.org,b@x.org", "--send-account", "gmail",
                                     "--send-rate-limit", "3"], {"UNI_MAIL_MCP_SEND_ALLOW": "@y.org"})
        policy = captured["send"]
        self.assertEqual((policy.allow, policy.account, policy.max_per_hour), (("a@x.org", "b@x.org", "@y.org"), "gmail", 3))

    def test_bad_entry_stops_the_start(self):
        code, captured = self.run_main(["--enable-send", "--send-allow", "nonsense"])
        self.assertEqual(code, 1)
        self.assertNotIn("send", captured)


if __name__ == "__main__":
    unittest.main()
