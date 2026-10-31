"""Tests for uni-maild against the fake IMAP server and token endpoint."""

from __future__ import annotations

import dataclasses
import imaplib
import json
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1]))

from fake_imap import FakeIMAPServer, FakeTokenEndpoint, Mailbox, Message  # noqa: E402
from uni_mail import auth, config  # noqa: E402
from uni_mail.client import Client, MailServiceError  # noqa: E402
from uni_mail.server import Service, SocketServer  # noqa: E402

USER = "student@example.edu"
PDF = b"%PDF-1.4 fake document " * 200


def wait_for(condition, timeout: float = 10.0, message: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}")


def sample_messages() -> list[Message]:
    today = date.today()
    return [
        Message(1, today - timedelta(days=40), "Old notice", flags={"\\Seen"}),
        Message(2, today - timedelta(days=3), "Homework 4", body="Submit homework 4 by Friday.\r\n"),
        Message(3, today - timedelta(days=1), "Exam moved", flags={"\\Seen"}, body="short",
                html="<html><head><style>p{}</style></head><body><p>The exam moved to <b>Monday</b>.</p>"
                     + "<p>" + "Details. " * 80 + "</p></body></html>"),
        Message(4, today, "Lab report", body="Attached is the lab rubric.\r\n",
                attachments=[("rubric é.pdf", "application/pdf", PDF)]),
    ]


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.box = Mailbox(user=USER, token="good-access", messages=sample_messages())
        self.imap = FakeIMAPServer(self.box)
        self.tokens = FakeTokenEndpoint(code="good-code", access_token="good-access")
        provider = dataclasses.replace(auth.PROVIDERS["microsoft"], token_endpoint=self.tokens.url)
        self.providers = mock.patch.dict(auth.PROVIDERS, {"microsoft": provider})
        self.providers.start()
        self.service = None
        self.server = None

    def tearDown(self):
        if self.service:
            self.service.stop()
            if not self.service.threads:
                # A started worker can still use the store while it stops.
                self.service.store.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.providers.stop()
        self.imap.close()
        self.tokens.close()
        self.tmp.cleanup()

    def make_service(self, *, signed_in: bool = True, extract: bool = True, web_settings: bool = True, retention: int | None = None,
                     **account) -> Service:
        self.raw = raw = {
            "state_dir": str(self.root / "state"),
            "socket": str(self.root / "mail.sock"),
            "export_dir": str(self.root / "exports"),
            "extract_root": str(self.root / "home") if extract else None,
            "web_settings": web_settings,
            **({} if retention is None else {"event_retention_days": retention}),
            "accounts": {"uni": {"provider": "microsoft", "address": USER, "poll_seconds": 30, **account}},
        }
        cfg = config.parse(raw)
        if signed_in:
            auth.TokenStore(cfg.state_dir / "tokens").save("uni", {"refresh_token": "refresh-1"})
        port = self.imap.port
        self.service = Service(cfg, imap_factory=lambda host, _: imaplib.IMAP4("127.0.0.1", port, timeout=10))
        return self.service

    def start(self, **kwargs) -> Service:
        service = self.make_service(**kwargs)
        service.start()
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the first sync")
        return service

    def call(self, service: Service, **request):
        response = service.handle(request)
        self.assertTrue(response["ok"], response)
        return response

    def test_first_sync_reads_only_the_window_and_sends_no_events(self):
        service = self.start()
        listed = self.call(service, op="list")
        self.assertEqual([m["subject"] for m in listed["messages"]], ["Lab report", "Exam moved", "Homework 4"])
        self.assertEqual(self.call(service, op="events")["events"], [])
        record = service.store.get(listed["messages"][2]["id"])
        self.assertIn("Submit homework 4", record["preview"])
        exam = service.store.get(listed["messages"][1]["id"])
        self.assertIn("The exam moved to Monday", exam["preview"])
        self.assertNotIn("p{}", exam["preview"])

    def test_download_rules(self):
        self.start()
        commands = [command.split(" ", 1)[1] for command in self.box.commands if " " in command]
        fetches = [command for command in commands if command.upper().startswith("UID FETCH")]
        self.assertTrue(fetches)
        for command in fetches:
            self.assertNotRegex(command, r"BODY\[", command)
            self.assertNotIn("RFC822)", command)
            self.assertNotRegex(command, r"BODY\.PEEK\[\]", command)
            self.assertNotRegex(command, r"\bRFC822\b(?!\.SIZE)", command)
        # Previews read only the start of the text part.
        previews = [command for command in fetches if re.search(r"BODY\.PEEK\[\d", command)]
        self.assertTrue(previews)
        self.assertTrue(all(re.search(r"<0\.\d+>", command) for command in previews), previews)
        # The old message outside the window is never read.
        self.assertFalse(any(re.match(r"UID FETCH 1 ", command) for command in fetches))
        self.assertFalse(any(command.upper().startswith(("SELECT", "CLOSE", "EXPUNGE", "UID STORE")) for command in commands))
        self.assertEqual({m.uid for m in self.box.messages if "\\Seen" in m.flags}, {1, 3})

    def test_new_mail_makes_one_event(self):
        service = self.start()
        wait_for(lambda: self.box.idling.is_set(), message="IDLE")
        self.box.deliver(Message(5, date.today(), "Quiz tomorrow", body="Quiz on chapter 3.\r\n"))
        events = wait_for(lambda: self.call(service, op="events")["events"], message="a new-mail event")
        self.assertEqual(len(events), 1)
        [event] = events
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual((event["event_type"], event["account"]), ("mail.new", "uni"))
        self.assertRegex(event["event_id"], r"^[0-9a-f-]{36}$")
        record = service.store.get(event["mail_id"])
        self.assertEqual(record["subject"], "Quiz tomorrow")
        self.assertEqual(event["data"]["subject"], "Quiz tomorrow")
        self.assertEqual(event["data"]["folder"], "INBOX")
        self.assertEqual(event["data"]["attachment_count"], 0)
        # Metadata only: never the text of the mail.
        self.assertNotIn("Quiz on chapter 3", json.dumps(event))
        self.assertNotIn("preview", event["data"])
        # The log is read with a cursor and keeps the event ID.
        self.assertEqual(self.call(service, op="events", after=event["seq"])["events"], [])
        self.assertEqual(self.call(service, op="events")["events"][0]["event_id"], event["event_id"])
        self.assertEqual(self.call(service, op="event_head")["first_pending"], event["seq"])
        self.assertEqual(self.call(service, op="events_consumed", upto=event["seq"])["consumed"], 1)
        head = self.call(service, op="event_head")
        self.assertEqual((head["first_pending"], head["last"]), (None, event["seq"]))

    def test_events_wait_for_new_mail(self):
        service = self.start()
        wait_for(lambda: self.box.idling.is_set(), message="IDLE")
        threading.Timer(0.3, lambda: self.box.deliver(Message(5, date.today(), "Late", body="x\r\n"))).start()
        response = self.call(service, op="events", after=0, timeout=10)
        self.assertEqual([event["data"]["subject"] for event in response["events"]], ["Late"])

    def test_events_refuses_a_bad_cursor(self):
        service = self.make_service()
        self.assertFalse(service.handle({"op": "events_consumed", "upto": "all"})["ok"])
        self.assertFalse(service.handle({"op": "ack", "seqs": [1]})["ok"])

    def test_a_message_is_never_indexed_without_its_event(self):
        service = self.make_service()
        record = {"id": "uni.0123456789abcdef", "account": "uni", "folder": "INBOX", "uidvalidity": 1, "uid": 9, "parts": []}
        with mock.patch.object(service.store, "_insert_event", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                service.store.add(record, ("mail.new", {"subject": "x"}))
        # Nothing was written, so the next sync sees the UID as new and makes the event then.
        self.assertIsNone(service.store.get(record["id"]))
        service.store.add(record, ("mail.new", {"subject": "x"}))
        self.assertEqual([row["mail_id"] for row in service.store.events_after(0)], [record["id"]])

    def test_event_log_is_pruned_but_keeps_the_newest_event(self):
        service = self.make_service()
        first = service.store.add_event("mail.error", "uni", detail="old", data={"detail": "old"})
        second = service.store.add_event("mail.error", "uni", detail="newer", data={"detail": "newer"})
        service.store._write("UPDATE events SET created=?", (time.time() - 30 * 86400,))
        self.assertEqual(service.store.prune_events(14), 1)
        self.assertEqual([row["seq"] for row in service.store.events_after(0)], [second["seq"]])
        self.assertGreater(second["seq"], first["seq"])
        self.assertEqual(service.store.prune_events(0), 0)

    def test_notes_on_messages(self):
        service = self.start()
        exam, homework = [m["id"] for m in self.call(service, op="list")["messages"][1:3]]
        first = self.call(service, op="note", id=exam, text="  kept unread: the exam moved  ", author="mail-triage")["note"]
        self.assertEqual((first["mail_id"], first["account"], first["author"], first["text"]),
                         (exam, "uni", "mail-triage", "kept unread: the exam moved"))
        self.call(service, op="note", id=homework, text="archived")
        newest_first = self.call(service, op="notes")["notes"]
        self.assertEqual([note["text"] for note in newest_first], ["archived", "kept unread: the exam moved"])
        self.assertEqual([note["text"] for note in self.call(service, op="notes", id=exam)["notes"]],
                         ["kept unread: the exam moved"])
        self.assertEqual(self.call(service, op="notes", accounts=["other"])["count"], 0)
        self.assertEqual(self.call(service, op="notes", accounts="uni", limit=1)["count"], 1)
        # A note outlives its message in the index, and one can be added after the message
        # left it (archiving removes it); old notes are pruned.
        service.store.remove([exam])
        self.assertEqual(self.call(service, op="notes", id=exam)["count"], 1)
        self.call(service, op="note", id=exam, text="archived after all")
        self.assertEqual(self.call(service, op="notes", id=exam)["count"], 2)
        service.store._write("DELETE FROM notes WHERE text=?", ("archived after all",))
        service.store._write("UPDATE notes SET created=? WHERE mail_id=?", (time.time() - 100 * 86400, exam))
        self.assertEqual(self.call(service, op="notes", since="30d")["count"], 1)
        self.assertEqual(service.store.prune_notes(90), 1)
        self.assertEqual(self.call(service, op="notes")["count"], 1)

    def test_notes_refuse_bad_input(self):
        service = self.start()
        mail = self.call(service, op="list")["messages"][0]["id"]
        for request in ({"id": "other.0000000000000000", "text": "x"}, {"id": "uni.nothex", "text": "x"},
                        {"id": mail, "text": "  "},
                        {"id": mail, "text": "x" * 2001}, {"id": mail, "text": "x", "author": "a\nb"}):
            self.assertFalse(service.handle({"op": "note", **request})["ok"], request)
        self.assertFalse(service.handle({"op": "notes", "accounts": [1]})["ok"])

    def test_refresh_token_rotation_is_saved(self):
        service = self.start()
        token_file = self.root / "state" / "tokens" / "uni.json"
        saved = json.loads(token_file.read_text())
        self.assertNotEqual(saved["refresh_token"], "refresh-1")
        self.assertIn(saved["refresh_token"], self.tokens.valid_refresh)
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        # A new daemon on the same state signs in with the rotated token, without a new login.
        service.stop()
        self.tokens.valid_refresh = {saved["refresh_token"]}
        cfg = config.parse(self.raw)
        port = self.imap.port
        again = Service(cfg, imap_factory=lambda host, _: imaplib.IMAP4("127.0.0.1", port, timeout=10))
        try:
            again.start()
            wait_for(lambda: again.accounts["uni"].status == "idle", message="the sync with the rotated token")
        finally:
            again.stop()

    def test_show_attachments_and_export(self):
        service = self.start()
        lab = self.call(service, op="list", query="Lab report")["messages"][0]
        shown = self.call(service, op="show", id=lab["id"])
        self.assertIn("lab rubric", shown["body"])
        self.assertEqual(shown["attachments"][0]["name"], "rubric é.pdf")
        self.assertEqual(shown["attachments"][0]["content_type"], "application/pdf")
        exported = self.call(service, op="export_attachment", id=lab["id"], index=0)
        self.assertEqual(Path(exported["path"]).read_bytes(), PDF)
        self.assertTrue(exported["path"].startswith(str(self.root / "exports")))
        again = self.call(service, op="export_attachment", id=lab["id"], index=0)
        self.assertTrue(again["cached"])
        extracted = self.call(service, op="extract_attachment", id=lab["id"], index=0, output="docs/rubric.pdf")
        self.assertEqual(Path(extracted["path"]).read_bytes(), PDF)
        refused = service.handle({"op": "extract_attachment", "id": lab["id"], "index": 0, "output": "/etc/passwd"})
        self.assertFalse(refused["ok"])
        missing = service.handle({"op": "export_attachment", "id": lab["id"], "index": 3})
        self.assertIn("out of range", missing["error"])
        # Reads never change the read state.
        self.assertNotIn("\\Seen", self.box.messages[3].flags)

    def test_html_body_is_used_when_plain_is_almost_empty(self):
        service = self.start()
        exam = self.call(service, op="list", query="Exam moved")["messages"][0]
        body = self.call(service, op="show", id=exam["id"])["body"]
        self.assertIn("The exam moved to Monday", body)

    def test_mark_read_and_unread(self):
        service = self.start()
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        before = len(self.box.commands)
        result = self.call(service, op="mark", ids=[homework["id"]], read=True)
        self.assertEqual(result["results"][homework["id"]], {"ok": True, "read": True})
        self.assertIn("\\Seen", self.box.messages[1].flags)
        self.assertTrue(service.store.get(homework["id"])["read"])
        self.call(service, op="mark", ids=[homework["id"]], read=False)
        self.assertNotIn("\\Seen", self.box.messages[1].flags)
        commands = [command.split(" ", 1)[1].upper() for command in self.box.commands[before:]]
        self.assertTrue(any(command.startswith("SELECT") for command in commands))
        self.assertTrue(any(command.startswith("UNSELECT") for command in commands))
        self.assertFalse(any(command.startswith(("CLOSE", "EXPUNGE")) for command in commands))

    def test_mark_reports_unknown_ids(self):
        service = self.start()
        response = service.handle({"op": "mark", "ids": ["uni.0000000000000000"], "read": True})
        self.assertFalse(response["ok"])
        self.assertIn("message not found", response["error"])

    def test_folders_lists_selectable_folders_with_their_special_use(self):
        self.box.listing = [
            '(\\HasNoChildren) "/" "INBOX"',
            '(\\HasNoChildren \\Archive) "/" "Archive"',
            '(\\Noselect \\HasChildren) "/" "[Gmail]"',
            '(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
            '(\\HasNoChildren) "/" "Clases/Programaci&APM-n"',
        ]
        service = self.start(archive_folder="Archive")
        before = len(self.box.commands)
        result = self.call(service, op="folders", account="uni")
        self.assertEqual(result["folders"], [
            {"name": "INBOX", "special": "inbox"},
            {"name": "Archive", "special": "archive"},
            {"name": "[Gmail]/All Mail", "special": "all"},
            {"name": "Clases/Programación", "special": ""},
        ])
        self.assertEqual((result["archive_folder"], result["synced"]), ("Archive", ["INBOX"]))
        commands = [command.split(" ", 1)[1].upper() for command in self.box.commands[before:]]
        self.assertTrue(any(command.startswith("LIST") for command in commands))
        self.assertFalse(any(command.startswith(("SELECT", "UID", "STORE", "CLOSE", "EXPUNGE")) for command in commands))

    def test_folders_rejects_an_unknown_account(self):
        service = self.start()
        response = service.handle({"op": "folders", "account": "nope"})
        self.assertFalse(response["ok"])
        self.assertIn("unknown account", response["error"])

    def test_archive_moves_the_message_with_uid_move(self):
        service = self.start(archive_folder="Archive")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        before = len(self.box.commands)
        result = self.call(service, op="archive", ids=[homework["id"]])
        self.assertEqual(result["results"][homework["id"]], {"ok": True, "folder": "Archive"})
        commands = [command.split(" ", 1)[1].upper() for command in self.box.commands[before:]]
        self.assertTrue(any(command.startswith("UID MOVE") for command in commands))
        self.assertFalse(any(command.startswith(("UID COPY", "UID STORE", "UID EXPUNGE", "CLOSE", "EXPUNGE")) for command in commands))
        # The message left the synced folder and left the folder mailing list.
        self.assertIsNone(service.store.get(homework["id"]))
        self.assertNotIn(2, {m.uid for m in self.box.messages})
        self.assertIn(2, {m.uid for m in self.box.folders["Archive"]})

    def test_trash_moves_the_message_to_the_trash_folder(self):
        service = self.start(trash_folder="Trash")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        before = len(self.box.commands)
        result = self.call(service, op="trash", ids=[homework["id"]])
        self.assertEqual(result["results"][homework["id"]], {"ok": True, "folder": "Trash"})
        commands = [command.split(" ", 1)[1].upper() for command in self.box.commands[before:]]
        self.assertTrue(any(command.startswith("UID MOVE") for command in commands))
        self.assertFalse(any(command.startswith(("UID STORE", "UID EXPUNGE", "CLOSE", "EXPUNGE")) for command in commands))
        self.assertIsNone(service.store.get(homework["id"]))
        self.assertIn(2, {m.uid for m in self.box.folders["Trash"]})

    def test_trash_already_in_the_trash_folder_is_refused(self):
        service = self.start(trash_folder="INBOX")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        response = service.handle({"op": "trash", "ids": [homework["id"]]})
        self.assertFalse(response["ok"])
        self.assertIn("already in the trash folder", response["error"])

    def test_archive_falls_back_to_copy_store_expunge_without_move(self):
        self.box.capabilities = "IMAP4rev1 IDLE UIDPLUS UNSELECT AUTH=XOAUTH2 AUTH=PLAIN"
        service = self.start(archive_folder="Archive")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        before = len(self.box.commands)
        result = self.call(service, op="archive", ids=[homework["id"]])
        self.assertEqual(result["results"][homework["id"]], {"ok": True, "folder": "Archive"})
        commands = [command.split(" ", 1)[1].upper() for command in self.box.commands[before:]]
        self.assertTrue(any(command.startswith("UID COPY") for command in commands))
        self.assertTrue(any(command.startswith("UID STORE") and "\\DELETED" in command for command in commands))
        self.assertTrue(any(command.startswith("UID EXPUNGE") for command in commands))
        self.assertFalse(any(command.startswith(("UID MOVE", "CLOSE")) or command == "EXPUNGE" for command in commands))
        self.assertNotIn(2, {m.uid for m in self.box.messages})
        self.assertIn(2, {m.uid for m in self.box.folders["Archive"]})

    def test_archive_reports_when_the_server_supports_neither_move_nor_uidplus(self):
        self.box.capabilities = "IMAP4rev1 IDLE UNSELECT AUTH=XOAUTH2 AUTH=PLAIN"
        service = self.start(archive_folder="Archive")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        response = service.handle({"op": "archive", "ids": [homework["id"]]})
        self.assertFalse(response["ok"])
        self.assertIn("neither MOVE nor UIDPLUS", response["error"])
        # Nothing moved, and the message stays in the index.
        self.assertIn(2, {m.uid for m in self.box.messages})
        self.assertIsNotNone(service.store.get(homework["id"]))

    def test_archive_without_an_archive_folder_is_refused(self):
        # The imap provider has no default archive_folder, unlike microsoft
        # and google.
        self.box.password = "app-password"
        password = self.root / "password"
        password.write_text("app-password\n")
        service = self.make_service(
            signed_in=False, provider="imap", auth="password", host="127.0.0.1", password_file=str(password),
        )
        service.start()
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the first sync")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        response = service.handle({"op": "archive", "ids": [homework["id"]]})
        self.assertFalse(response["ok"])
        self.assertIn("no archive folder is configured", response["error"])

    def test_archive_already_in_the_archive_folder_is_refused(self):
        service = self.start(archive_folder="INBOX")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        response = service.handle({"op": "archive", "ids": [homework["id"]]})
        self.assertFalse(response["ok"])
        self.assertIn("already in the archive folder", response["error"])

    def test_revoked_sign_in_waits_for_a_new_login(self):
        self.tokens.revoked = True
        service = self.make_service()
        service.start()
        wait_for(lambda: service.accounts["uni"].status == "auth_required", message="auth_required")
        events = self.call(service, op="events")["events"]
        self.assertEqual([event["event_type"] for event in events], ["mail.auth"])
        self.assertIn("invalid_grant", events[0]["data"]["detail"])
        self.assertEqual(events[0]["mail_id"], "")
        # One event only, even when the worker checks again.
        time.sleep(0.5)
        self.assertEqual(len(service.store.events_after(0)), 1)

        self.tokens.revoked = False
        begin = self.call(service, op="auth_begin", account="uni")
        self.assertIn("client_id=9e5f94bc-e8a4-4e73-b8be-63364c29d753", begin["url"])
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(begin["url"]).query)["state"][0]
        wrong = service.handle({"op": "auth_finish", "account": "uni", "redirect": "https://localhost/?code=good-code&state=x"})
        self.assertFalse(wrong["ok"])
        self.call(service, op="auth_finish", account="uni", redirect=f"https://localhost/?code=good-code&state={state}")
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the sync after the new sign-in")
        token_file = self.root / "state" / "tokens" / "uni.json"
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_unexpected_error_does_not_stop_the_worker(self):
        service = self.make_service()
        account = service.accounts["uni"]
        calls = []
        original = account.sync

        def broken_once(imap):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            return original(imap)

        account.sync = broken_once
        with mock.patch("uni_mail.account.log"):
            service.start()
            wait_for(lambda: account.status == "error", message="the error state")
            self.assertIn("database is locked", account.error)
            account.wake()
            wait_for(lambda: account.status == "idle", message="the recovery")

    def test_uidvalidity_change_starts_a_new_baseline(self):
        service = self.start()
        old_ids = {m["id"] for m in self.call(service, op="list")["messages"]}
        self.box.uidvalidity = 2
        account = service.accounts["uni"]
        imap = account.connect()
        try:
            account.sync(imap)
        finally:
            imap.logout()
        new_ids = {m["id"] for m in self.call(service, op="list")["messages"]}
        self.assertEqual(len(new_ids), 3)
        self.assertFalse(old_ids & new_ids)
        self.assertEqual(service.store.events_after(0), [])

    def test_password_account(self):
        self.box.password = "app-password"
        password = self.root / "password"
        password.write_text("app-password\n")
        service = self.make_service(signed_in=False, provider="google", auth="password", password_file=str(password))
        service.start()
        wait_for(lambda: service.accounts["uni"].status == "idle", message="a password sign-in")
        self.assertEqual(self.call(service, op="list")["count"], 3)

    def fail(self, service: Service, **request) -> str:
        response = service.handle(request)
        self.assertFalse(response["ok"], response)
        return response["error"]

    def test_settings_change_an_account_without_a_restart(self):
        service = self.start()
        worker = service.accounts["uni"]
        settings = self.call(service, op="settings")
        self.assertTrue(settings["editable"])
        [uni] = settings["accounts"]
        self.assertEqual((uni["source"], uni["changed"], uni["settings"]["sync_days"]), ("base", False, 7))
        values = {**uni["settings"], "sync_days": 14}
        self.call(service, op="settings_save", account="uni", settings=values)
        self.assertIsNot(service.accounts["uni"], worker)
        self.assertEqual(service.accounts["uni"].cfg.sync_days, 14)
        self.assertTrue(worker.stop.is_set())
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the sync with the new settings")
        # The address did not change, so the sign-in and the index stay.
        self.assertEqual(self.call(service, op="list")["count"], 3)
        [uni] = self.call(service, op="settings")["accounts"]
        self.assertEqual((uni["changed"], uni["settings"]["sync_days"], uni["base"]["sync_days"]), (True, 14, 7))
        # The change stays after a restart.
        service.stop()
        self.service = Service(config.parse(self.raw), imap_factory=service.imap_factory)
        self.assertEqual(self.service.accounts["uni"].cfg.sync_days, 14)
        self.call(self.service, op="settings_reset", account="uni")
        self.assertEqual(self.service.accounts["uni"].cfg.sync_days, 7)
        self.assertFalse(self.call(self.service, op="settings")["accounts"][0]["changed"])

    def test_settings_add_and_remove_a_password_account(self):
        service = self.start()
        self.box.password = "app-password"
        values = {"provider": "imap", "address": USER, "host": "127.0.0.1", "folders": ["INBOX"]}
        self.assertIn("password_file", self.fail(service, op="settings_save", account="other", settings=values))
        self.call(service, op="settings_save", account="other", settings=values, password="app-password")
        stored = self.root / "state" / "passwords" / "other"
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual(service.accounts["other"].cfg.password_file, str(stored))
        wait_for(lambda: service.accounts["other"].status == "idle", message="the new account")
        self.assertEqual(self.call(service, op="list", account="other")["count"], 3)
        self.assertEqual(self.call(service, op="settings")["accounts"][0]["password"], "socket")
        self.call(service, op="settings_delete", account="other")
        self.assertNotIn("other", service.accounts)
        self.assertFalse(stored.exists())
        self.assertEqual(self.call(service, op="list", account="")["count"], 3)
        # A base account is only marked as removed, and it can come back.
        self.call(service, op="settings_delete", account="uni")
        self.assertEqual(service.accounts, {})
        [uni] = self.call(service, op="settings")["accounts"]
        self.assertTrue(uni["removed"])
        self.call(service, op="settings_reset", account="uni")
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the restored account")

    def test_settings_cannot_send_secrets_to_another_server(self):
        password = self.root / "password"
        password.write_text("app-password\n")
        service = self.make_service(provider="microsoft")
        values = self.call(service, op="settings")["accounts"][0]["settings"]
        error = self.fail(service, op="settings_save", account="uni", settings={**values, "host": "evil.example"})
        self.assertIn("host of its provider", error)
        self.fail(service, op="settings_save", account="uni", settings={**values, "password_file": str(password)})
        self.fail(service, op="settings_save", account="Bad name", settings=values)
        self.fail(service, op="settings_save", account="uni", settings={**values, "port": 0})
        self.fail(service, op="settings_save", account="uni", settings={**values, "folders": []})
        self.assertEqual(service.accounts["uni"].host, "outlook.office365.com")

    def test_settings_keep_a_base_password_only_for_the_same_server(self):
        password = self.root / "password"
        password.write_text("app-password\n")
        service = self.make_service(signed_in=False, provider="imap", host="127.0.0.1", password_file=str(password))
        values = self.call(service, op="settings")["accounts"][0]["settings"]
        self.call(service, op="settings_save", account="uni", settings={**values, "sync_days": 3})
        self.assertEqual(service.accounts["uni"].cfg.password_file, str(password))
        error = self.fail(service, op="settings_save", account="uni", settings={**values, "host": "evil.example"})
        self.assertIn("password_file", error)
        self.assertEqual(service.accounts["uni"].cfg.host, "127.0.0.1")

    def test_settings_can_be_turned_off(self):
        service = self.make_service(web_settings=False)
        values = self.call(service, op="settings")["accounts"][0]["settings"]
        self.assertFalse(self.call(service, op="settings")["editable"])
        self.assertIn("web_settings", self.fail(service, op="settings_save", account="uni", settings=values))

    def test_socket_client(self):
        service = self.start()
        self.server = SocketServer(self.root / "mail.sock", service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        client = Client(str(self.root / "mail.sock"))
        status = client.status()["accounts"][0]
        self.assertEqual((status["name"], status["status"], status["messages"], status["unread"]), ("uni", "idle", 3, 2))
        self.assertEqual(client.list(unread=True)["count"], 2)
        self.assertEqual(client.list(since="2d")["count"], 2)
        with self.assertRaises(MailServiceError) as raised:
            client.show("uni.missing")
        self.assertIn("message not found", str(raised.exception))
        self.assertEqual((self.root / "mail.sock").stat().st_mode & 0o777, 0o660)
        self.assertNotIn("refresh", json.dumps(client.status()))

    def test_client_reports_a_stopped_service(self):
        with self.assertRaises(MailServiceError) as raised:
            Client(str(self.root / "missing.sock")).status()
        self.assertIn("not running", str(raised.exception))



if __name__ == "__main__":
    unittest.main()
