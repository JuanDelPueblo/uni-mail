"""One IMAP account: the sync worker and the on-demand actions.

Download rules (docs/DESIGN.md):

- The sync reads only the messages inside the sync window (UID SEARCH SINCE).
- It reads flags, sizes, BODYSTRUCTURE, six header fields and a text preview
  of a few KiB. It never fetches a full message.
- All reads use BODY.PEEK, and the sync opens folders read-only (EXAMINE).
- A read-state change opens the folder read-write for one UID STORE. The
  worker then leaves the folder with UNSELECT, never CLOSE, because CLOSE
  expunges messages that have the \\Deleted flag.
"""

from __future__ import annotations

import imaplib
import logging
import os
import socket
import ssl
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import auth, events, mime
from .config import Account
from .imapproto import (
    ParseError, Part, attachment_parts, chunks, compact_set, imap_date, parse_bodystructure, parse_fetch, parse_list,
    quote_mailbox, section, text_parts,
)
from .store import Store, mail_id

log = logging.getLogger("uni_mail")

HEADER_ITEM = f"BODY.PEEK[HEADER.FIELDS ({' '.join(mime.HEADER_FIELDS)})]"
PREVIEW_CHARS = 4000
BODY_CHARS = 100_000
BODY_RAW_LIMIT = 1024 * 1024
IDLE_LIMIT = 25 * 60
ERROR_NOTICE_SECONDS = 30 * 60
# An account that waits for a new sign-in still tries again after this time,
# because a server can refuse a sign-in for a time, for example with a rate
# limit. The retry sends no second event.
AUTH_RETRY_SECONDS = 6 * 3600
SPECIAL_USE = ("archive", "all", "drafts", "sent", "junk", "trash", "flagged")
NETWORK_ERRORS = (OSError, ssl.SSLError, socket.timeout, imaplib.IMAP4.abort)


class MailError(Exception):
    """An action failed. The message is the error text for the caller."""


def default_imap_factory(host: str, port: int) -> imaplib.IMAP4:
    return imaplib.IMAP4_SSL(host, port, timeout=60, ssl_context=ssl.create_default_context())


def _check(result: tuple[str, list[Any]], what: str) -> list[Any]:
    typ, data = result
    if typ != "OK":
        detail = b" ".join(item for item in data if isinstance(item, bytes)).decode(errors="replace")
        raise imaplib.IMAP4.error(f"{what} failed: {typ} {detail}".strip())
    return data


def utc_iso(value: str) -> str:
    """Return an ISO time in UTC, so that index times sort as strings."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class MailAccount:
    def __init__(
        self, cfg: Account, store: Store, tokens: auth.TokenStore,
        imap_factory: Callable[[str, int], imaplib.IMAP4] = default_imap_factory,
    ):
        self.cfg = cfg
        self.name = cfg.name
        self.store = store
        self.tokens = tokens
        self.imap_factory = imap_factory
        self.provider = auth.PROVIDERS.get(cfg.provider)
        self.host = cfg.host or (self.provider.host if self.provider else "")
        self.port = cfg.port
        self.session = auth.OAuthSession(cfg.name, self.provider, tokens) if cfg.auth == "oauth" else None
        self.status = "starting"
        self.error = ""
        self.error_since = 0.0
        self.error_noticed = False
        self.stop = threading.Event()
        self._wake = threading.Event()
        self._auth_generation = -1.0
        self._auth_failed_at = 0.0
        self._action_lock = threading.Lock()
        self._action_imap: imaplib.IMAP4 | None = None

    # Connection

    def _credential_generation(self) -> float:
        if self.session:
            return self.tokens.generation(self.name)
        try:
            return os.stat(self.cfg.password_file).st_mtime
        except OSError:
            return 0.0

    def connect(self) -> imaplib.IMAP4:
        for attempt in range(2):
            imap = self.imap_factory(self.host, self.port)
            try:
                if self.session:
                    token = self.session.access_token()
                    imap.authenticate("XOAUTH2", auth.xoauth2(self.cfg.address, token))
                else:
                    try:
                        password = Path(self.cfg.password_file).read_text().strip()
                    except OSError as error:
                        raise auth.AuthError(f"cannot read the password file: {error}") from error
                    imap.login(self.cfg.address, password)
            except imaplib.IMAP4.error as error:
                _logout(imap)
                # A token can stop working before its expiry time. Get a new
                # one and try one more time before the account waits for a
                # new sign-in.
                if self.session and attempt == 0:
                    self.session.forget()
                    continue
                raise auth.AuthError(f"the IMAP server refused the sign-in: {error}") from error
            except BaseException:
                _logout(imap)
                raise
            typ, data = imap.capability()
            if typ == "OK" and data and data[-1]:
                imap.capabilities = tuple(data[-1].decode().upper().split())
            return imap
        raise AssertionError("unreachable")

    def _set(self, status: str, error: str = "") -> None:
        if status != "error":
            self.error_since, self.error_noticed = 0.0, False
        elif not self.error_since:
            self.error_since = time.time()
        if status != self.status or error != self.error:
            log.info("account %s: %s%s", self.name, status, f": {error}" if error else "")
        self.status, self.error = status, error

    # Worker

    def run(self) -> None:
        backoff = 30.0
        while not self.stop.is_set():
            if (
                self.status == "auth_required"
                and self._credential_generation() == self._auth_generation
                and time.time() - self._auth_failed_at < AUTH_RETRY_SECONDS
            ):
                self._sleep(15)
                continue
            try:
                imap = self.connect()
            except auth.AuthError as error:
                self._auth_generation = self._credential_generation()
                self._auth_failed_at = time.time()
                if self.status != "auth_required":
                    self.store.add_event("mail.auth", self.name, detail=str(error), data={"detail": str(error)})
                self._set("auth_required", str(error))
                continue
            except (auth.TransientAuthError, *NETWORK_ERRORS, imaplib.IMAP4.error) as error:
                backoff = self._failed(error, backoff)
                continue
            except Exception as error:  # noqa: BLE001 - the worker thread must not stop
                log.exception("account %s: unexpected error during the sign-in", self.name)
                backoff = self._failed(error, backoff)
                continue
            try:
                while not self.stop.is_set():
                    self._set("syncing")
                    self.sync(imap)
                    self._set("idle")
                    backoff = 30.0
                    self.wait_for_change(imap)
            except (*NETWORK_ERRORS, imaplib.IMAP4.error, ParseError, auth.TransientAuthError) as error:
                backoff = self._failed(error, backoff)
            except Exception as error:  # noqa: BLE001 - the worker thread must not stop
                log.exception("account %s: unexpected error during the sync", self.name)
                backoff = self._failed(error, backoff)
            finally:
                _logout(imap)

    def _failed(self, error: BaseException, backoff: float) -> float:
        self._set("error", str(error) or type(error).__name__)
        if not self.error_noticed and time.time() - self.error_since > ERROR_NOTICE_SECONDS:
            self.error_noticed = True
            self.store.add_event("mail.error", self.name, detail=self.error, data={"detail": self.error})
        self._sleep(backoff)
        return min(backoff * 2, 600.0)

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def wake(self) -> None:
        """Stop a wait between attempts, for example after a new sign-in."""
        self._wake.set()

    def wait_for_change(self, imap: imaplib.IMAP4) -> None:
        """Wait for new mail with IDLE on the first folder, or sleep."""
        if "IDLE" not in imap.capabilities:
            self._sleep(self.cfg.poll_seconds)
            return
        _check(imap.select(quote_mailbox(self.cfg.folders[0]), readonly=True), "EXAMINE")
        with imap.idle(duration=min(self.cfg.poll_seconds, IDLE_LIMIT)) as idler:
            for typ, _ in idler:
                if typ in ("EXISTS", "EXPUNGE", "FETCH", "RECENT") or self.stop.is_set():
                    break

    def sync(self, imap: imaplib.IMAP4) -> None:
        self.store.drop_other_folders(self.name, self.cfg.folders)
        for folder in self.cfg.folders:
            self.sync_folder(imap, folder)

    def sync_folder(self, imap: imaplib.IMAP4, folder: str) -> None:
        _check(imap.select(quote_mailbox(folder), readonly=True), f"EXAMINE {folder}")
        uidvalidity = _uidvalidity(imap)
        row = self.store.folder(self.name, folder)
        if row is None or row["uidvalidity"] != uidvalidity:
            self.store.reset_folder(self.name, folder, uidvalidity)
            baseline = False
        else:
            baseline = bool(row["baseline"])

        since = date.today() - timedelta(days=self.cfg.sync_days)
        found = _check(imap.uid("SEARCH", "SINCE", imap_date(since)), "UID SEARCH")
        window = {int(uid) for uid in (found[0] or b"").split()} if found else set()
        known = self.store.uids(self.name, folder)
        self.store.remove(known[uid] for uid in known if uid not in window)

        for batch in chunks([uid for uid in window if uid not in known], 25):
            for record in self.fetch_metadata(imap, folder, uidvalidity, batch):
                event = None
                if baseline:
                    count = len(attachment_parts([Part.from_dict(value) for value in record["parts"]]))
                    event = ("mail.new", events.mail_data(record, count))
                self.store.add(record, event)

        for batch in chunks([uid for uid in window if uid in known], 250):
            data = _check(imap.uid("FETCH", compact_set(batch), "(UID FLAGS)"), "FETCH FLAGS")
            for response in parse_fetch(data):
                uid = int(response["UID"])
                if uid in known:
                    flags = {str(flag).lower() for flag in response.get("FLAGS") or []}
                    self.store.set_flags(known[uid], "\\seen" in flags, "\\flagged" in flags)
        self.store.finish_sync(self.name, folder)

    def fetch_metadata(self, imap: imaplib.IMAP4, folder: str, uidvalidity: int, uids: list[int]) -> list[dict[str, Any]]:
        items = f"(UID FLAGS INTERNALDATE RFC822.SIZE BODYSTRUCTURE {HEADER_ITEM})"
        data = _check(imap.uid("FETCH", compact_set(uids), items), "FETCH metadata")
        records = []
        for response in sorted(parse_fetch(data), key=lambda item: int(item["UID"])):
            uid = int(response["UID"])
            header = section(response, "BODY[HEADER")
            headers = mime.parse_headers(header if isinstance(header, bytes) else str(header or "").encode())
            try:
                parts = parse_bodystructure(response.get("BODYSTRUCTURE"))
            except ParseError as error:
                log.warning("account %s: uid %s has a BODYSTRUCTURE that cannot be parsed: %s", self.name, uid, error)
                parts = []
            flags = {str(flag).lower() for flag in response.get("FLAGS") or []}
            arrived = utc_iso(mime.internal_date(str(response.get("INTERNALDATE") or ""))) or utc_iso(headers["date"])
            records.append({
                "id": mail_id(self.name, folder, uidvalidity, uid),
                "account": self.name,
                "folder": folder,
                "uidvalidity": uidvalidity,
                "uid": uid,
                "message_id": headers["message_id"],
                "date": arrived,
                "sender": headers["sender"],
                "recipients": headers["to"],
                "cc": headers["cc"],
                "subject": headers["subject"],
                "read": "\\seen" in flags,
                "flagged": "\\flagged" in flags,
                "size": int(response.get("RFC822.SIZE") or 0),
                "parts": [part.to_dict() for part in parts],
                "preview": self._preview(imap, uid, parts),
            })
        return records

    def _preview(self, imap: imaplib.IMAP4, uid: int, parts: list[Part]) -> str:
        plain, html = text_parts(parts)
        part = plain or html
        # Use the HTML part when the plain part is almost empty, as choose_body does.
        if plain and html and plain.size < 500 and html.size > max(400, plain.size * 2):
            part = html
        if part is None:
            return ""
        limit = 4096 if part is plain else 16384
        if part.encoding == "base64":
            limit = limit * 4 // 3
        data = _check(imap.uid("FETCH", str(uid), f"(BODY.PEEK[{part.number}]<0.{limit}>)"), "FETCH preview")
        responses = parse_fetch(data)
        raw = section(responses[0], f"BODY[{part.number}]") if responses else None
        if not isinstance(raw, bytes):
            raw = str(raw or "").encode()
        partial = part.size > limit
        text = mime.decode_text(mime.decode_transfer(raw, part.encoding, partial=partial), part.charset, partial=partial)
        if part is html:
            text = mime.html_to_text(text)
        return " ".join(text.split())[:PREVIEW_CHARS]

    # Actions. They use a second connection, so the worker can stay in IDLE.

    def _with_action(self, operation: Callable[[imaplib.IMAP4], Any]) -> Any:
        with self._action_lock:
            for attempt in range(2):
                try:
                    if self._action_imap is None:
                        self._action_imap = self.connect()
                    return operation(self._action_imap)
                except NETWORK_ERRORS as error:
                    # The server closes an unused connection after some time.
                    self._drop_action()
                    if attempt:
                        raise MailError(f"IMAP connection failed: {error}") from error
                except auth.AuthError as error:
                    self._drop_action()
                    raise MailError(f"sign-in failed: {error}") from error
                except auth.TransientAuthError as error:
                    self._drop_action()
                    raise MailError(str(error)) from error
                except imaplib.IMAP4.error as error:
                    self._drop_action()
                    raise MailError(str(error)) from error

    def _drop_action(self) -> None:
        if self._action_imap is not None:
            _logout(self._action_imap)
            self._action_imap = None

    def close(self) -> None:
        self.stop.set()
        self._wake.set()
        with self._action_lock:
            self._drop_action()

    def _open(self, imap: imaplib.IMAP4, record: dict[str, Any], writable: bool) -> None:
        folder = record["folder"]
        _check(imap.select(quote_mailbox(folder), readonly=not writable), f"{'SELECT' if writable else 'EXAMINE'} {folder}")
        if _uidvalidity(imap) != record["uidvalidity"]:
            raise MailError(f"the folder {folder} changed on the server; wait for the next sync")

    def list_folders(self) -> list[dict[str, Any]]:
        """The folders that the server lets the account select, with the special use of each: archive, all, drafts, sent,
        junk, trash, flagged or inbox. A read-only LIST."""

        def operation(imap: imaplib.IMAP4) -> list[dict[str, Any]]:
            folders = parse_list(_check(imap.list(), "LIST"))
            for folder in folders:
                names = {flag.lower() for flag in folder.pop("flags")}
                folder["special"] = next((use for use in SPECIAL_USE if f"\\{use}" in names), "")
                if folder["name"].upper() == "INBOX":
                    folder["special"] = "inbox"
            return folders

        try:
            return self._with_action(operation)
        except ParseError as error:
            raise MailError(f"cannot read the folder list: {error}") from error

    def set_read(self, records: list[dict[str, Any]], read: bool) -> dict[str, dict[str, Any]]:
        """Add or remove \\Seen. Return a result for each mail ID."""
        by_folder: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for record in records:
            by_folder.setdefault((record["folder"], record["uidvalidity"]), []).append(record)

        def operation(imap: imaplib.IMAP4) -> dict[str, dict[str, Any]]:
            results: dict[str, dict[str, Any]] = {}
            for group in by_folder.values():
                self._open(imap, group[0], writable=True)
                try:
                    uids = [record["uid"] for record in group]
                    _check(imap.uid("STORE", compact_set(uids), "+FLAGS.SILENT" if read else "-FLAGS.SILENT", "(\\Seen)"), "STORE")
                    data = _check(imap.uid("FETCH", compact_set(uids), "(UID FLAGS)"), "FETCH FLAGS")
                    flags = {int(response["UID"]): {str(flag).lower() for flag in response.get("FLAGS") or []} for response in parse_fetch(data)}
                finally:
                    _leave(imap, group[0]["folder"])
                for record in group:
                    current = flags.get(record["uid"])
                    if current is None:
                        results[record["id"]] = {"ok": False, "error": "the message is no longer on the server"}
                        continue
                    is_read = "\\seen" in current
                    self.store.set_flags(record["id"], is_read, "\\flagged" in current)
                    results[record["id"]] = {"ok": is_read == read, "read": is_read}
                    if is_read != read:
                        results[record["id"]]["error"] = "the server did not change the read state"
            return results

        return self._with_action(operation)

    def archive(self, records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Move messages to the account's archive folder. Return a result for each mail ID."""
        return self._move(records, self.cfg.archive_folder, "archive")

    def trash(self, records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Move messages to the account's trash folder, which the provider empties later (Gmail and
        Outlook after about 30 days). Never a permanent delete. Return a result for each mail ID."""
        return self._move(records, self.cfg.trash_folder, "trash")

    def _move(self, records: list[dict[str, Any]], destination: str, kind: str) -> dict[str, dict[str, Any]]:
        """Move messages to `destination`, the account's `kind` folder (archive or trash).

        Uses UID MOVE (RFC 6851) when the server offers it, which is one
        atomic command; Gmail and Office365 both support it. Otherwise it
        falls back to UID COPY, then UID STORE +\\Deleted, then UID EXPUNGE
        of exactly those UIDs (RFC 4315 UIDPLUS), never a bare EXPUNGE or
        CLOSE, so it can never remove any other \\Deleted message from the
        folder. A message that moves out of a synced folder also leaves the
        local index; the next sync of the destination folder indexes it
        again only if that folder is itself configured to sync.
        """
        if not destination:
            return {
                record["id"]: {"ok": False, "error": f"no {kind} folder is configured for this account"}
                for record in records
            }
        by_folder: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for record in records:
            by_folder.setdefault((record["folder"], record["uidvalidity"]), []).append(record)

        def operation(imap: imaplib.IMAP4) -> dict[str, dict[str, Any]]:
            results: dict[str, dict[str, Any]] = {}
            for group in by_folder.values():
                folder = group[0]["folder"]
                if folder == destination:
                    for record in group:
                        results[record["id"]] = {"ok": False, "error": f"the message is already in the {kind} folder"}
                    continue
                self._open(imap, group[0], writable=True)
                uid_set = compact_set([record["uid"] for record in group])
                try:
                    if "MOVE" in imap.capabilities:
                        _check(imap.uid("MOVE", uid_set, quote_mailbox(destination)), "UID MOVE")
                    elif "UIDPLUS" in imap.capabilities:
                        _check(imap.uid("COPY", uid_set, quote_mailbox(destination)), "UID COPY")
                        _check(imap.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)"), "STORE")
                        _check(imap.uid("EXPUNGE", uid_set), "UID EXPUNGE")
                    else:
                        raise MailError(f"the server supports neither MOVE nor UIDPLUS; cannot move to {destination}")
                finally:
                    _leave(imap, folder)
                ids = [record["id"] for record in group]
                self.store.remove(ids)
                for identifier in ids:
                    results[identifier] = {"ok": True, "folder": destination}
            return results

        return self._with_action(operation)

    def fetch_part(self, record: dict[str, Any], part: Part, limit: int | None = None) -> bytes:
        """Fetch and decode one MIME part. With a limit, fetch only its start."""
        max_bytes = self.cfg.max_part_mb * 1024 * 1024
        if limit is None and part.size > max_bytes:
            raise MailError(f"the part is {part.size // 1048576} MiB; the limit is {self.cfg.max_part_mb} MiB")
        item = f"BODY.PEEK[{part.number}]" + (f"<0.{limit}>" if limit else "")

        def operation(imap: imaplib.IMAP4) -> bytes:
            self._open(imap, record, writable=False)
            data = _check(imap.uid("FETCH", str(record["uid"]), f"({item})"), "FETCH part")
            responses = parse_fetch(data)
            if not responses:
                raise MailError("the message is no longer on the server")
            raw = section(responses[0], f"BODY[{part.number}]")
            return raw if isinstance(raw, bytes) else str(raw or "").encode()

        raw = self._with_action(operation)
        return mime.decode_transfer(raw, part.encoding, partial=bool(limit) and part.size > (limit or 0))

    def fetch_message(self, record: dict[str, Any]) -> bytes:
        """The whole raw message. BODY.PEEK[] leaves the read state and every other flag as they are."""
        max_bytes = self.cfg.max_part_mb * 1024 * 1024
        if record["size"] > max_bytes:
            raise MailError(f"the message is {record['size'] // 1048576} MiB; the limit is {self.cfg.max_part_mb} MiB")

        def operation(imap: imaplib.IMAP4) -> bytes:
            self._open(imap, record, writable=False)
            data = _check(imap.uid("FETCH", str(record["uid"]), "(BODY.PEEK[])"), "FETCH message")
            responses = parse_fetch(data)
            if not responses:
                raise MailError("the message is no longer on the server")
            raw = section(responses[0], "BODY[]")
            return raw if isinstance(raw, bytes) else str(raw or "").encode()

        return self._with_action(operation)

    def body(self, record: dict[str, Any]) -> str:
        cached = self.store.read_cache(record["id"], "body.txt")
        if cached is not None:
            return cached.decode("utf-8", errors="replace")
        parts = [Part.from_dict(value) for value in record["parts"]]
        plain, html = text_parts(parts)

        def text(part: Part | None) -> str:
            if part is None:
                return ""
            data = self.fetch_part(record, part, BODY_RAW_LIMIT)
            return mime.decode_text(data, part.charset, partial=part.size > BODY_RAW_LIMIT)

        plain_text = text(plain)
        html_text = mime.html_to_text(text(html)) if html else ""
        text = mime.choose_body(plain_text, html_text)[:BODY_CHARS]
        self.store.write_cache(record["id"], "body.txt", text.encode(), self.name, self.cfg.cache_limit_mb * 1024 * 1024)
        return text


def attachments(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Attachment list with stable zero-based indexes."""
    result = []
    for index, part in enumerate(attachment_parts([Part.from_dict(value) for value in record["parts"]])):
        size = part.size * 3 // 4 if part.encoding == "base64" else part.size
        result.append({
            "index": index,
            "name": part.filename or f"attachment-{index}",
            "size": size,
            "content_type": part.content_type,
            "disposition": part.disposition or "inline",
            "content_id": part.content_id,
        })
    return result


def attachment_part(record: dict[str, Any], index: int) -> Part:
    parts = attachment_parts([Part.from_dict(value) for value in record["parts"]])
    if not 0 <= index < len(parts):
        raise MailError(f"attachment index {index} is out of range; the message has {len(parts)} attachment(s)")
    return parts[index]


def _uidvalidity(imap: imaplib.IMAP4) -> int:
    _, data = imap.response("UIDVALIDITY")
    try:
        return int(data[-1])
    except (TypeError, ValueError, IndexError):
        raise imaplib.IMAP4.error("the server sent no UIDVALIDITY") from None


def _leave(imap: imaplib.IMAP4, folder: str) -> None:
    """Leave a read-write folder without CLOSE, which would expunge."""
    if "UNSELECT" in imap.capabilities:
        imap.unselect()
    else:
        imap.select(quote_mailbox(folder), readonly=True)


def _logout(imap: imaplib.IMAP4) -> None:
    try:
        imap.logout()
    except (*NETWORK_ERRORS, imaplib.IMAP4.error):
        pass
