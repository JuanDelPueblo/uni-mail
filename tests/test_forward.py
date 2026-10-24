"""Tests for forwarding: the daemon action with fake IMAP and SMTP, the CLI, and the MCP tool with its protections.

No test opens a real SMTP connection: the daemon gets a fake SMTP factory.
"""

from __future__ import annotations

import dataclasses
import imaplib
import json
import sys
import tempfile
import unittest
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1]))

from fake_imap import FakeIMAPServer, FakeTokenEndpoint, Mailbox, Message  # noqa: E402
from test_mcp import McpTestCase, needs_mcp  # noqa: E402
from test_send import FakeSMTP  # noqa: E402
from test_service import PDF, USER, sample_messages, wait_for  # noqa: E402
from uni_mail import auth, cli, config, mcp_server, send  # noqa: E402
from uni_mail.client import Client  # noqa: E402
from uni_mail.server import Service  # noqa: E402

GMAIL = "tony@gmail.example"
FRIEND = "friend@example.org"


def html_only() -> Message:
    original = EmailMessage()
    original["Date"] = f"{date.today().strftime('%a, %d %b %Y')} 09:00:00 +0000"
    original["From"] = "Registrar <registrar@example.edu>"
    original["To"] = USER
    original["Subject"] = "Fwd: Fw: Fees due"
    original["Message-ID"] = "<9@example.edu>"
    original.set_content("<html><head><style>p{color:red}</style><script>alert('x')</script></head>"
                         "<body><p>Fees are <b>due</b> Friday.</p><img src='http://tracker.example/p.gif'></body></html>",
                         subtype="html")
    return Message(9, date.today(), "Fwd: Fw: Fees due", sender="Registrar <registrar@example.edu>", _raw=original.as_bytes())


class ForwardServiceTest(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances, FakeSMTP.refuse_auth, FakeSMTP.refuse = [], False, {}
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.box = Mailbox(user=USER, token="good-access", messages=[*sample_messages(), html_only()])
        self.imap = FakeIMAPServer(self.box)
        self.tokens = FakeTokenEndpoint(code="good-code", access_token="good-access")
        provider = dataclasses.replace(auth.PROVIDERS["microsoft"], token_endpoint=self.tokens.url)
        google = dataclasses.replace(auth.PROVIDERS["google"], token_endpoint=self.tokens.url)
        self.providers = mock.patch.dict(auth.PROVIDERS, {"microsoft": provider, "google": google})
        self.providers.start()
        raw = {
            "state_dir": str(self.root / "state"), "socket": str(self.root / "mail.sock"),
            "export_dir": str(self.root / "exports"), "extract_root": None,
            "accounts": {"uni": {"provider": "microsoft", "address": USER, "poll_seconds": 30},
                         "gmail": {"provider": "google", "address": GMAIL, "poll_seconds": 30},
                         "pw": {"provider": "imap", "address": "pw@example.org", "host": "localhost", "password_file": "/x"}},
        }
        cfg = config.parse(raw)
        for name in ("uni", "gmail"):
            auth.TokenStore(cfg.state_dir / "tokens").save(name, {"refresh_token": "refresh-1"})
        port = self.imap.port
        self.service = Service(cfg, imap_factory=lambda host, _: imaplib.IMAP4("127.0.0.1", port, timeout=10), smtp_factory=FakeSMTP)
        with mock.patch("uni_mail.account.log"):
            self.service.start()
            wait_for(lambda: self.service.accounts["uni"].status == "idle", message="the first sync")
        # The sender account "gmail" cannot sign in to the fake IMAP server (another user); it only sends.
        self.ids = {mail["subject"]: mail["id"] for mail in self.service.handle({"op": "list", "account": "uni"})["messages"]}

    def tearDown(self):
        self.service.stop()
        if not self.service.threads:
            self.service.store.close()
        self.providers.stop()
        self.imap.close()
        self.tokens.close()
        self.tmp.cleanup()

    def forward(self, subject="Homework 4", account="gmail", to=FRIEND, **request):
        return self.service.handle({"op": "forward", "id": self.ids[subject], "account": account, "to": to, **request})

    def sent(self, index=0):
        return FakeSMTP.instances[index].sent[0]

    def test_forward_builds_a_new_message_from_the_sender_account(self):
        response = self.forward(note="For your records")
        self.assertTrue(response["ok"], response)
        self.assertEqual((response["forwarded"], response["to"], response["from"]), (True, [FRIEND], GMAIL))
        (smtp,) = FakeSMTP.instances
        self.assertEqual(smtp.host, "smtp.gmail.com")
        message, sender, envelope = self.sent()
        self.assertEqual((sender, envelope), (GMAIL, [FRIEND]))
        self.assertEqual((message["From"], message["To"]), (GMAIL, FRIEND))
        self.assertEqual(message["Subject"], "Fwd: Homework 4")
        self.assertEqual(message["Reply-To"], "sender@example.com")
        self.assertIsNone(message["In-Reply-To"])
        self.assertIsNone(message["Bcc"])
        text = message.get_body(("plain",)).get_content()
        lines = text.splitlines()
        self.assertEqual(lines[0], f"Forwarded from {USER}")
        self.assertEqual(lines[1], "From: Sender <sender@example.com>")
        self.assertTrue(lines[2].startswith("Date: "))
        self.assertEqual(lines[3], "Subject: Homework 4")
        self.assertEqual(lines[4], "Note: For your records")
        self.assertEqual(lines[5], "")
        self.assertIn("Submit homework 4 by Friday.", text)
        self.assertLess(text.index("Submit homework"), 200)

    def test_the_original_stays_unread_and_is_fetched_with_peek(self):
        self.assertTrue(self.forward()["ok"])
        original = next(item for item in self.box.messages if item.uid == 2)
        self.assertNotIn("\\Seen", original.flags)
        self.assertFalse(self.service.store.get(self.ids["Homework 4"])["read"])
        fetches = [command for command in self.box.commands if "FETCH" in command]
        self.assertTrue(any("BODY.PEEK[]" in command for command in fetches), fetches)
        self.assertFalse([command for command in fetches if "BODY[]" in command or "RFC822" in command.replace("RFC822.SIZE", "")],
                         fetches)
        self.assertFalse([command for command in self.box.commands if "STORE" in command or "MOVE" in command or "COPY" in command])

    def test_subject_prefix_is_not_doubled(self):
        self.assertTrue(self.forward(subject="Fwd: Fw: Fees due")["ok"])
        self.assertEqual(self.sent()[0]["Subject"], "Fwd: Fees due")
        for original, expected in (("Hello", "Fwd: Hello"), ("FWD: hello", "Fwd: hello"), ("Fw: Fwd: x", "Fwd: x"),
                                   ("Re: plan", "Fwd: Re: plan"), ("", "Fwd: (no subject)"), ("Fwdx: a", "Fwd: Fwdx: a")):
            with self.subTest(original=original):
                self.assertEqual(send.forward_subject(original), expected)

    def test_html_only_original_becomes_readable_text(self):
        self.assertTrue(self.forward(subject="Fwd: Fw: Fees due")["ok"])
        message = self.sent()[0]
        self.assertFalse(message.is_multipart())
        self.assertEqual(message.get_content_type(), "text/plain")
        text = message.get_content()
        self.assertIn("Fees are due Friday.", text)
        for hidden in ("<p>", "alert", "color:red", "tracker.example"):
            self.assertNotIn(hidden, text)

    def test_html_message_with_a_thin_plain_part_uses_the_html_text(self):
        self.assertTrue(self.forward(subject="Exam moved")["ok"])
        self.assertIn("The exam moved to Monday.", self.sent()[0].get_content())

    def test_attachments_are_kept(self):
        response = self.forward(subject="Lab report")
        self.assertTrue(response["ok"], response)
        self.assertNotIn("attachments_omitted", response)
        message = self.sent()[0]
        (attachment,) = list(message.iter_attachments())
        self.assertEqual((attachment.get_content_type(), attachment.get_filename()), ("application/pdf", "rubric é.pdf"))
        self.assertEqual(attachment.get_content(), PDF)
        self.assertIn("Attachments: rubric é.pdf", message.get_body(("plain",)).get_content())

    def test_attachments_over_the_limit_are_omitted_and_named(self):
        with mock.patch.object(send, "MAX_FORWARD_ATTACHMENTS", 100):
            response = self.forward(subject="Lab report")
        self.assertTrue(response["ok"], response)
        self.assertEqual(len(response["attachments_omitted"]), 1)
        message = self.sent()[0]
        self.assertEqual(list(message.iter_attachments()), [])
        text = message.get_content()
        self.assertIn("Not forwarded", text)
        self.assertIn("rubric é.pdf", text)

    def test_a_message_over_the_part_limit_goes_without_its_attachments(self):
        record = self.service.store.get(self.ids["Lab report"])
        with mock.patch.dict(record, {"size": 40 * 1024 * 1024}), mock.patch.object(self.service.store, "get", return_value=record):
            response = self.forward(subject="Lab report")
        self.assertTrue(response["ok"], response)
        self.assertEqual(len(response["attachments_omitted"]), 1)
        self.assertEqual(list(self.sent()[0].iter_attachments()), [])
        self.assertIn("Attached is the lab rubric.", self.sent()[0].get_content())

    def test_the_same_mail_is_not_forwarded_to_the_same_address_twice(self):
        self.assertTrue(self.forward()["ok"])
        again = self.forward(to=FRIEND.upper())
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["forwarded"])
        self.assertEqual(again["already_forwarded"], [FRIEND.upper()])
        self.assertIn("already forwarded", again["message"])
        self.assertEqual(len(FakeSMTP.instances), 1)

    def test_a_new_recipient_still_gets_it_and_the_old_one_is_skipped(self):
        self.assertTrue(self.forward()["ok"])
        response = self.forward(to=[FRIEND, "other@example.org"])
        self.assertEqual((response["forwarded"], response["to"], response["already_forwarded"]), (True, ["other@example.org"], [FRIEND]))
        self.assertEqual(FakeSMTP.instances[1].sent[0][2], ["other@example.org"])

    def test_force_forwards_again(self):
        self.assertTrue(self.forward()["ok"])
        self.assertTrue(self.forward(force=True)["forwarded"])
        self.assertEqual(len(FakeSMTP.instances), 2)

    def test_another_mail_to_the_same_address_is_fine(self):
        self.assertTrue(self.forward()["ok"])
        self.assertTrue(self.forward(subject="Exam moved")["forwarded"])

    def test_a_failed_send_is_not_remembered(self):
        FakeSMTP.refuse_auth = True
        self.assertFalse(self.forward()["ok"])
        FakeSMTP.refuse_auth = False
        self.assertTrue(self.forward()["forwarded"])

    def test_a_refused_recipient_is_not_remembered(self):
        FakeSMTP.refuse = {FRIEND: 1}
        response = self.forward(to=[FRIEND, "other@example.org"])
        self.assertIn(FRIEND, response["refused"])
        FakeSMTP.refuse = {}
        self.assertTrue(self.forward()["forwarded"])
        self.assertFalse(self.forward(to="other@example.org")["forwarded"])

    def test_the_ledger_holds_ids_and_addresses_only(self):
        self.assertTrue(self.forward()["ok"])
        (line,) = (self.service.cfg.state_dir / "forwarded.jsonl").read_text().splitlines()
        entry = json.loads(line)
        self.assertEqual((entry["mail_id"], entry["to"], entry["account"]), (self.ids["Homework 4"], FRIEND, "gmail"))
        self.assertNotIn("Submit homework", line)
        self.assertEqual((self.service.cfg.state_dir / "forwarded.jsonl").stat().st_mode & 0o777, 0o600)

    def test_bad_requests_send_nothing(self):
        cases = (dict(account="nope"), dict(account="pw"), dict(to=[]), dict(to="not an address"),
                 dict(to=['"a@example.org" <b@example.org>', "c@example.org", "d"]), dict(note="x" * 2000), dict(note=5))
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(self.forward(**case)["ok"])
        response = self.service.handle({"op": "forward", "id": "uni.0000000000000000", "account": "gmail", "to": [FRIEND]})
        self.assertFalse(response["ok"])
        self.assertFalse(FakeSMTP.instances)

    def test_forward_can_use_the_account_that_holds_the_mail(self):
        self.assertTrue(self.forward(account="uni")["ok"])
        self.assertEqual(self.sent()[1], USER)

    def test_the_log_has_the_recipients_and_never_the_text(self):
        with self.assertLogs("uni_mail", "INFO") as logs:
            self.assertTrue(self.forward(note="SECRET-NOTE")["ok"])
        text = "\n".join(logs.output)
        for wanted in (FRIEND, "Fwd: Homework 4", "account=gmail", self.ids["Homework 4"]):
            self.assertIn(wanted, text)
        for secret in ("Submit homework", "SECRET-NOTE", "good-access"):
            self.assertNotIn(secret, text)


class CliTest(unittest.TestCase):
    def test_parser_reads_the_forward_command(self):
        args = cli.parser().parse_args(["forward", "uni.1", "--account", "gmail", "--to", "a@x.org", "--to", "b@x.org",
                                        "--note", "FYI", "--force"])
        self.assertEqual((args.mail_id, args.account, args.to, args.note, args.force), ("uni.1", "gmail", ["a@x.org", "b@x.org"], "FYI", True))

    def test_account_and_recipient_are_required(self):
        for argv in (["forward", "uni.1", "--to", "a@x.org"], ["forward", "uni.1", "--account", "g"], ["forward", "--account", "g", "--to", "a@x.org"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                cli.parser().parse_args(argv)

    def test_forward_goes_through_the_client(self):
        args = cli.parser().parse_args(["--config", "/nonexistent.json", "--socket", "/nonexistent.sock", "forward", "uni.1",
                                        "--account", "gmail", "--to", "a@x.org"])
        self.assertFalse(args.force)
        with mock.patch.object(Client, "forward", return_value={"ok": True}) as forwarded:
            cli.run(args)
        forwarded.assert_called_once_with("uni.1", "gmail", ["a@x.org"], "", False)

    def test_the_client_sends_force_only_when_asked(self):
        with mock.patch.object(Client, "request", return_value={"ok": True}) as request:
            Client("/x").forward("uni.1", "gmail", ["a@x.org"], "hi", True)
            Client("/x").forward("uni.1", "gmail", ["a@x.org"])
        self.assertEqual(request.call_args_list[0].args[0],
                         {"op": "forward", "id": "uni.1", "account": "gmail", "to": ["a@x.org"], "note": "hi", "force": True})
        self.assertEqual(request.call_args_list[1].args[0], {"op": "forward", "id": "uni.1", "account": "gmail", "to": ["a@x.org"]})


class PolicyTest(unittest.TestCase):
    def test_forwarding_needs_a_fixed_sender(self):
        with self.assertRaisesRegex(ValueError, "--send-account"):
            mcp_server.SendPolicy(("a@x.org",)).forward_sender()
        self.assertEqual(mcp_server.SendPolicy(account="gmail").forward_sender(), "gmail")

    def test_note_limits(self):
        policy = mcp_server.SendPolicy(max_body=50)
        self.assertEqual(policy.check_note(" hi "), "hi")
        self.assertEqual(policy.check_note(None), "")
        for bad in ("x" * 51, 5):
            with self.assertRaises(ValueError):
                policy.check_note(bad)


@needs_mcp
class McpForwardTest(McpTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.daemon.responses["forward"] = lambda request: {
            "ok": True, "id": request["id"], "forwarded": True, "account": request["account"], "message_id": "<2@x>",
            "to": request["to"], "subject": "Fwd: Hello"}
        self.daemon.responses["send"] = {"ok": True}

    def policy(self, **policy):
        policy.setdefault("allow", ("tony@example.org",))
        policy.setdefault("account", "gmail")
        return mcp_server.SendPolicy(**policy)

    async def forward(self, arguments, accounts=(), **policy):
        from mcp import Client as McpClient

        server = mcp_server.build_server(self.daemon.path, send=self.policy(**policy), accounts=accounts)
        async with McpClient(server) as client:
            return await client.call_tool("mail_forward", arguments)

    def forwarded_requests(self):
        return [request for request in self.daemon.requests if request["op"] == "forward"]

    async def test_off_by_default(self):
        from mcp import Client as McpClient

        async with McpClient(mcp_server.build_server(self.daemon.path)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
        self.assertNotIn("mail_forward", names)

    async def test_enabled_adds_the_tool_and_keeps_the_others(self):
        from mcp import Client as McpClient

        async with McpClient(mcp_server.build_server(self.daemon.path, send=self.policy())) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        self.assertEqual(set(tools), {*self.tool_names(), "mail_send", "mail_forward"})
        schema = tools["mail_forward"].input_schema["properties"]
        self.assertEqual(set(schema), {"mail_id", "to", "note"})
        self.assertFalse(tools["mail_forward"].annotations.read_only_hint)
        self.assertIn("pre-approved", tools["mail_forward"].description)

    def tool_names(self):
        from test_mcp import TOOLS

        return TOOLS

    async def test_allowed_forward_reaches_the_daemon_from_the_fixed_sender(self):
        result = await self.forward({"mail_id": " university.1 ", "to": ["tony@example.org"], "note": " FYI "})
        self.assertFalse(result.is_error, self.text(result))
        (request,) = self.forwarded_requests()
        self.assertEqual(request, {"op": "forward", "id": "university.1", "account": "gmail", "to": ["tony@example.org"], "note": "FYI"})

    async def test_a_scoped_server_forwards_only_its_accounts_mail(self):
        result = await self.forward({"mail_id": "outlook.1", "to": ["tony@example.org"]}, accounts=("university",))
        self.assertTrue(result.is_error)
        self.assertEqual(self.forwarded_requests(), [])
        result = await self.forward({"mail_id": "university.1", "to": ["tony@example.org"]}, accounts=("university",))
        self.assertFalse(result.is_error, self.text(result))
        self.assertEqual(self.forwarded_requests()[0]["account"], "gmail")

    async def test_force_and_account_cannot_be_passed(self):
        result = await self.forward({"mail_id": "university.1", "to": ["tony@example.org"], "force": True, "account": "other"})
        self.assertTrue(result.is_error or all("force" not in request for request in self.forwarded_requests()))
        for request in self.forwarded_requests():
            self.assertNotIn("force", request)
            self.assertEqual(request["account"], "gmail")

    async def test_recipient_outside_the_allowlist_is_refused(self):
        result = await self.forward({"mail_id": "university.1", "to": ["tony@example.org", "evil@example.org"]})
        self.assertTrue(result.is_error)
        self.assertIn("evil@example.org", self.text(result))
        self.assertFalse(self.forwarded_requests())

    async def test_empty_allowlist_refuses_everything(self):
        result = await self.forward({"mail_id": "university.1", "to": ["tony@example.org"]}, allow=())
        self.assertTrue(result.is_error)
        self.assertIn("allowlist is empty", self.text(result))
        self.assertFalse(self.forwarded_requests())

    async def test_no_fixed_sender_refuses(self):
        result = await self.forward({"mail_id": "university.1", "to": ["tony@example.org"]}, account="")
        self.assertTrue(result.is_error)
        self.assertIn("--send-account", self.text(result))
        self.assertFalse(self.forwarded_requests())

    async def test_rate_limit_is_shared_with_mail_send(self):
        from mcp import Client as McpClient

        policy = self.policy(max_per_hour=2)
        async with McpClient(mcp_server.build_server(self.daemon.path, send=policy)) as client:
            results = [await client.call_tool("mail_forward", {"mail_id": f"university.{n}", "to": ["tony@example.org"]})
                       for n in range(2)]
            results.append(await client.call_tool("mail_send", {"to": ["tony@example.org"], "subject": "s", "body": "b"}))
            results.append(await client.call_tool("mail_forward", {"mail_id": "university.9", "to": ["tony@example.org"]}))
        self.assertEqual([result.is_error for result in results], [False, False, True, True])
        self.assertIn("limit reached", self.text(results[3]))
        self.assertEqual(len(self.forwarded_requests()), 2)

    async def test_already_forwarded_is_a_clear_result(self):
        self.daemon.responses["forward"] = {"ok": True, "id": "university.1", "forwarded": False,
                                            "already_forwarded": ["tony@example.org"], "message": "university.1 was already forwarded"}
        result = await self.forward({"mail_id": "university.1", "to": ["tony@example.org"]})
        self.assertFalse(result.is_error)
        self.assertIn("already forwarded", self.text(result))

    async def test_daemon_refusal_is_a_tool_error(self):
        self.daemon.responses["forward"] = {"ok": False, "error": "message not found: university.1"}
        result = await self.forward({"mail_id": "university.1", "to": ["tony@example.org"]})
        self.assertTrue(result.is_error)
        self.assertIn("not found", self.text(result))

    async def test_forwards_are_logged_without_the_text(self):
        with self.assertLogs("uni_mail.send", "INFO") as logs:
            await self.forward({"mail_id": "university.1", "to": ["tony@example.org"], "note": "SECRET-NOTE"})
            await self.forward({"mail_id": "university.2", "to": ["evil@example.org"], "note": "SECRET-NOTE"})
        text = "\n".join(logs.output)
        self.assertIn("university.1", text)
        self.assertIn("evil@example.org", text)
        self.assertNotIn("SECRET-NOTE", text)


if __name__ == "__main__":
    unittest.main()
