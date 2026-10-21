"""uni-maild: the mail daemon. It runs one worker per account and a Unix socket.

The daemon owns the IMAP connections, the sign-in tokens, the index and the
event log. It makes no decision about mail and runs no external program;
`uni-mail dispatch` delivers its events.

Protocol: the client sends one JSON object and a newline. The service sends
one JSON object and a newline, then closes the connection. Every response has
"ok". A failed request has "error" with the text for the operator.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import auth, config, events, mime, send, settings
from .account import MailAccount, MailError, attachment_part, attachments, default_imap_factory
from .imapproto import Part, attachment_parts
from .store import Store

log = logging.getLogger("uni_mail")

REQUEST_LIMIT = 1024 * 1024
MAX_WAIT = 300
# The socket of a hermes-mail daemon. uni-maild does not start while one
# answers there, so the two never sync the same accounts side by side.
LEGACY_SOCKET = Path("/run/hermes-mail/mail.sock")


class RequestError(Exception):
    pass


def parse_time(value: str) -> str:
    """Return a UTC ISO time for 2d, 12h, 30m, 1w, yesterday or an ISO date."""
    now = datetime.now(timezone.utc)
    text = value.strip().lower()
    if text == "yesterday":
        local = datetime.now().astimezone()
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return midnight.astimezone(timezone.utc).isoformat()
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    if text[:-1].isdigit() and text[-1:] in units:
        return (now - timedelta(**{units[text[-1]]: int(text[:-1])})).isoformat()
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise RequestError(f"cannot read the time {value!r}; use 2d, 12h, yesterday or an ISO date") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(timezone.utc).isoformat()


class Service:
    def __init__(self, cfg: config.Config, imap_factory: Callable = default_imap_factory,
                 smtp_factory: Callable = send.default_smtp_factory):
        self.cfg = cfg
        self.imap_factory = imap_factory
        self.smtp_factory = smtp_factory
        self.store = Store(cfg.state_dir)
        self.tokens = auth.TokenStore(cfg.state_dir / "tokens")
        self.logins = auth.LoginFlow(self.tokens)
        self.settings = settings.Settings(cfg.state_dir, cfg.base) if cfg.web_settings else None
        self.settings_errors: dict[str, str] = {}
        self.accounts: dict[str, MailAccount] = {}
        self.threads: list[threading.Thread] = []
        self._started = False
        self._reconfigure = threading.Lock()
        self.apply()
        self.store.prune_events(cfg.event_retention_days)

    def _configured(self) -> dict[str, config.Account]:
        if self.settings is None:
            return dict(self.cfg.accounts)
        accounts, self.settings_errors = self.settings.accounts()
        for name, error in self.settings_errors.items():
            log.error("account %s: %s", name, error)
        return accounts

    def _launch(self, account: MailAccount) -> None:
        thread = threading.Thread(target=account.run, name=f"account-{account.name}", daemon=True)
        thread.start()
        self.threads = [item for item in self.threads if item.is_alive()] + [thread]

    def apply(self) -> None:
        """Run the configured accounts: start the new and changed ones, stop the
        removed ones. An unchanged account keeps its worker and connection."""
        with self._reconfigure:
            wanted = self._configured()
            accounts: dict[str, MailAccount] = {}
            for name, cfg in wanted.items():
                current = self.accounts.get(name)
                if current is not None and current.cfg == cfg:
                    accounts[name] = current
                    continue
                if current is not None:
                    current.close()
                    if any(getattr(current.cfg, key) != getattr(cfg, key) for key in ("provider", "address", "host", "port")):
                        # Another mailbox: its index and sign-in do not apply.
                        self.store.drop_other_folders(name, ())
                        if (current.cfg.provider, current.cfg.address) != (cfg.provider, cfg.address):
                            self.tokens.path(name).unlink(missing_ok=True)
                accounts[name] = MailAccount(cfg, self.store, self.tokens, self.imap_factory)
                if self._started:
                    self._launch(accounts[name])
            for name, current in self.accounts.items():
                if name not in accounts:
                    current.close()
            self.accounts = accounts
            self.store.drop_other_accounts(accounts)

    def start(self) -> None:
        with self._reconfigure:
            self._started = True
            for account in self.accounts.values():
                self._launch(account)

    def stop(self) -> None:
        with self._reconfigure:
            self._started = False
            for account in self.accounts.values():
                account.close()

    # Helpers

    def _account(self, name: str) -> MailAccount:
        try:
            return self.accounts[name]
        except KeyError:
            raise RequestError(f"unknown account {name!r}; accounts: {', '.join(sorted(self.accounts))}") from None

    def _record(self, identifier: Any) -> dict[str, Any]:
        record = self.store.get(str(identifier))
        if record is None:
            raise RequestError(f"message not found: {identifier} (it may be older than the sync window)")
        return record

    @staticmethod
    def _summary(record: dict[str, Any]) -> dict[str, Any]:
        return {key: record[key] for key in (
            "id", "account", "folder", "message_id", "date", "sender", "recipients", "cc", "subject", "read", "flagged", "size",
        )}

    # Operations

    def op_status(self, _: dict[str, Any]) -> dict[str, Any]:
        accounts = []
        for account in self.accounts.values():
            synced = self.store.last_sync(account.name)
            accounts.append({
                "name": account.name,
                "provider": account.cfg.provider,
                "address": account.cfg.address,
                "status": account.status,
                "error": account.error,
                "last_sync": datetime.fromtimestamp(synced, timezone.utc).isoformat() if synced else "",
                "sync_days": account.cfg.sync_days,
                "folders": list(account.cfg.folders),
                **self.store.counts(account.name),
            })
        return {"accounts": accounts}

    def op_accounts(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"accounts": {name: {"address": account.cfg.address, "provider": account.cfg.provider}
                             for name, account in self.accounts.items()}}

    def op_folders(self, request: dict[str, Any]) -> dict[str, Any]:
        account = self._account(str(request.get("account") or ""))
        return {"account": account.name, "folders": account.list_folders(),
                "archive_folder": account.cfg.archive_folder, "synced": list(account.cfg.folders)}

    def op_list(self, request: dict[str, Any]) -> dict[str, Any]:
        account = request.get("account") or ""
        if account:
            self._account(account)
        messages = self.store.query(
            account=account,
            since=parse_time(request["since"]) if request.get("since") else "",
            until=parse_time(request["until"]) if request.get("until") else "",
            unread=bool(request.get("unread")),
            text=str(request.get("query") or ""),
            limit=int(request.get("limit") or 50),
        )
        return {"count": len(messages), "messages": messages}

    def op_show(self, request: dict[str, Any]) -> dict[str, Any]:
        record = self._record(request.get("id"))
        account = self._account(record["account"])
        result = {**self._summary(record), "attachments": attachments(record)}
        if request.get("full", True):
            try:
                result["body"] = account.body(record)
            except MailError as error:
                result["body"] = record["preview"]
                result["body_error"] = f"the full text is not available ({error}); the body is the preview"
        else:
            result["body"] = record["preview"]
        return result

    def op_attachments(self, request: dict[str, Any]) -> dict[str, Any]:
        record = self._record(request.get("id"))
        return {"id": record["id"], "subject": record["subject"], "attachments": attachments(record)}

    def _attachment_data(self, request: dict[str, Any]) -> tuple[dict[str, Any], int, Any, bytes]:
        record = self._record(request.get("id"))
        index = int(request.get("index", -1))
        part = attachment_part(record, index)
        data = self._account(record["account"]).fetch_part(record, part)
        return record, index, part, data

    def op_export_attachment(self, request: dict[str, Any]) -> dict[str, Any]:
        record = self._record(request.get("id"))
        index = int(request.get("index", -1))
        part = attachment_part(record, index)
        directory = self.cfg.export_dir / record["id"]
        destination = directory / f"{index}-{mime.safe_filename(part.filename, index, part.content_type)}"
        if destination.is_file():
            cached = True
            size = destination.stat().st_size
        else:
            data = self._account(record["account"]).fetch_part(record, part)
            directory.mkdir(mode=0o750, parents=True, exist_ok=True)
            _write_new(destination, data, 0o640)
            cached, size = False, len(data)
        return {"path": str(destination), "filename": part.filename or destination.name, "mime_type": part.content_type,
                "size": size, "cached": cached}

    def op_extract_attachment(self, request: dict[str, Any]) -> dict[str, Any]:
        root = self.cfg.extract_root
        if root is None:
            raise RequestError("extract-attachment is off; use export-attachment")
        root = root.resolve()
        output = Path(str(request.get("output") or "")).expanduser()
        destination = (output if output.is_absolute() else root / output).resolve()
        if destination == root or not destination.is_relative_to(root):
            raise RequestError(f"the output path must be below {root}")
        if destination.exists():
            raise RequestError(f"the output path exists: {destination}")
        record, index, part, data = self._attachment_data(request)
        destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        _write_new(destination, data, 0o640)
        return {"path": str(destination), "filename": part.filename or destination.name, "mime_type": part.content_type, "size": len(data)}

    def op_mark(self, request: dict[str, Any]) -> dict[str, Any]:
        ids = request.get("ids")
        if not isinstance(ids, list) or not ids:
            raise RequestError("ids must be a non-empty list of mail IDs")
        read = bool(request.get("read", True))
        results: dict[str, dict[str, Any]] = {}
        by_account: dict[str, list[dict[str, Any]]] = {}
        for identifier in ids:
            record = self.store.get(str(identifier))
            if record is None:
                results[str(identifier)] = {"ok": False, "error": "message not found"}
            else:
                by_account.setdefault(record["account"], []).append(record)
        for name, records in by_account.items():
            try:
                results.update(self._account(name).set_read(records, read))
            except MailError as error:
                results.update({record["id"]: {"ok": False, "error": str(error)} for record in records})
        failed = {key: value for key, value in results.items() if not value.get("ok")}
        response: dict[str, Any] = {"results": results}
        if failed:
            response["ok"] = False
            response["error"] = "; ".join(f"{key}: {value.get('error', 'failed')}" for key, value in failed.items())
        return response

    def op_archive(self, request: dict[str, Any]) -> dict[str, Any]:
        ids = request.get("ids")
        if not isinstance(ids, list) or not ids:
            raise RequestError("ids must be a non-empty list of mail IDs")
        results: dict[str, dict[str, Any]] = {}
        by_account: dict[str, list[dict[str, Any]]] = {}
        for identifier in ids:
            record = self.store.get(str(identifier))
            if record is None:
                results[str(identifier)] = {"ok": False, "error": "message not found"}
            else:
                by_account.setdefault(record["account"], []).append(record)
        for name, records in by_account.items():
            try:
                results.update(self._account(name).archive(records))
            except MailError as error:
                results.update({record["id"]: {"ok": False, "error": str(error)} for record in records})
        failed = {key: value for key, value in results.items() if not value.get("ok")}
        response: dict[str, Any] = {"results": results}
        if failed:
            response["ok"] = False
            response["error"] = "; ".join(f"{key}: {value.get('error', 'failed')}" for key, value in failed.items())
        return response

    # The event log. `uni-mail dispatch` reads it with a cursor.

    def _event_out(self, row: dict[str, Any]) -> dict[str, Any]:
        data = row["data"]
        if not data:
            # An event from hermes-mail has no data. Fill it from the index while the mail is there.
            if row["kind"] == "mail.new":
                record = self.store.get(row["mail_id"])
                if record is not None:
                    count = len(attachment_parts([Part.from_dict(value) for value in record["parts"]]))
                    data = events.mail_data(record, count)
            elif row["detail"]:
                data = {"detail": row["detail"]}
        return events.build(event_id=row["event_id"], event_type=row["kind"], seq=row["seq"], account=row["account"],
                            mail_id=row["mail_id"], created=row["created"], data=data)

    def op_events(self, request: dict[str, Any]) -> dict[str, Any]:
        """The events after the sequence number `after`, oldest first. With
        `timeout`, wait up to that many seconds for one."""
        after = int(request.get("after") or 0)
        limit = int(request.get("limit") or 100)
        timeout = min(max(float(request.get("timeout") or 0), 0.0), MAX_WAIT)
        rows = self.store.wait_events(after, timeout, limit) if timeout else self.store.events_after(after, limit)
        return {"events": [self._event_out(row) for row in rows], **self.store.event_head()}

    def op_event_head(self, _: dict[str, Any]) -> dict[str, Any]:
        return self.store.event_head()

    def op_events_consumed(self, request: dict[str, Any]) -> dict[str, Any]:
        """The dispatcher has stored every event up to `upto`."""
        upto = request.get("upto")
        if not isinstance(upto, int) or isinstance(upto, bool):
            raise RequestError("upto must be an event sequence number")
        count = self.store.consume_events(upto)
        self.store.prune_events(self.cfg.event_retention_days)
        return {"consumed": count}

    def op_auth_begin(self, request: dict[str, Any]) -> dict[str, Any]:
        account = self._account(str(request.get("account") or ""))
        if not account.session:
            raise RequestError(f"account {account.name} uses a password, not OAuth")
        want_send = bool(request.get("send"))
        if want_send and not account.provider.send_scope:
            raise RequestError(f"account {account.name}: the {account.cfg.provider} sign-in scope already allows sending; "
                               "no extra consent is needed")
        return self.logins.begin(account.name, account.provider, account.cfg.address, want_send)

    def op_auth_finish(self, request: dict[str, Any]) -> dict[str, Any]:
        account = self._account(str(request.get("account") or ""))
        if not account.session:
            raise RequestError(f"account {account.name} uses a password, not OAuth")
        self.logins.finish(account.name, account.provider, str(request.get("redirect") or ""))
        account.session.forget()
        account.wake()
        return {"account": account.name, "message": "signed in; the account starts to sync in a few seconds"}

    # Sending

    def _sender(self, name: str) -> MailAccount:
        account = self._account(name)
        if not account.session or not account.provider.smtp_host:
            raise RequestError(f"account {account.name} cannot send mail: sending supports OAuth accounts of the "
                               "google and microsoft providers")
        return account

    def op_send_check(self, request: dict[str, Any]) -> dict[str, Any]:
        """Sign in to the SMTP server of an account and leave again. Sends nothing."""
        account = self._sender(str(request.get("account") or ""))
        send.check(account.cfg.address, account.provider, account.session, self.smtp_factory)
        return {"account": account.name, "address": account.cfg.address, "can_send": True,
                "smtp": f"{account.provider.smtp_host}:{account.provider.smtp_port}"}

    def op_send(self, request: dict[str, Any]) -> dict[str, Any]:
        account = self._sender(str(request.get("account") or ""))
        to = send.parse_addresses(request.get("to"), "to")
        cc = send.parse_addresses(request.get("cc"), "cc")
        bcc = send.parse_addresses(request.get("bcc"), "bcc")
        subject = send.single_line(request.get("subject") or "", "subject", send.MAX_SUBJECT)
        body = request.get("body")
        if not isinstance(body, str) or not body.strip():
            raise RequestError("body must be non-empty text")
        if len(body) > send.MAX_BODY_CHARS:
            raise RequestError(f"body is longer than {send.MAX_BODY_CHARS} characters")
        reference = ""
        if request.get("reply_to"):
            record = self._record(request["reply_to"])
            if record["account"] != account.name:
                raise RequestError(f"message {record['id']} belongs to account {record['account']}; reply from that account")
            reference = record["message_id"]
            subject = subject or send.reply_subject(record["subject"])
            if not to and not cc:
                to = send.parse_addresses(record["sender"], "the sender of the original")
        if not to:
            raise RequestError("to must name at least one recipient")
        if not subject:
            raise RequestError("subject must not be empty")
        envelope = list(dict.fromkeys(address for address in (*to, *cc, *bcc)))
        if len(envelope) > send.MAX_RECIPIENTS:
            raise RequestError(f"at most {send.MAX_RECIPIENTS} recipients")
        message = send.build_message(account.cfg.address, to, cc, subject, body, bool(request.get("html")), reference)
        refused = send.deliver(account.cfg.address, account.provider, account.session, message, envelope, self.smtp_factory)
        message_id = str(message["Message-ID"])
        # The body is never logged.
        log.info("sent mail: account=%s message_id=%s to=%s cc=%s bcc=%s subject=%r", account.name, message_id, to, cc, bcc,
                 subject[:200])
        result: dict[str, Any] = {"account": account.name, "from": account.cfg.address, "message_id": message_id,
                                  "to": to, "cc": cc, "bcc": len(bcc), "subject": subject}
        if refused:
            result["refused"] = refused
            log.warning("sent mail %s: the server refused %s", message_id, sorted(refused))
        return result

    def _settings(self) -> settings.Settings:
        if self.settings is None:
            raise RequestError("account changes are off; the web_settings setting of the config file turns them on")
        return self.settings

    def op_settings(self, _: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "editable": self.settings is not None,
            "providers": list(config.PROVIDERS),
            "default_hosts": {name: provider.host for name, provider in auth.PROVIDERS.items()},
            "default_archive_folders": dict(config.DEFAULT_ARCHIVE_FOLDER),
        }
        if self.settings is None:
            result["accounts"] = [{
                "name": name, "source": "base", "changed": False, "removed": False,
                "settings": settings.form(account), "base": settings.form(account),
                "password": "base" if account.password_file else "", "error": "",
            } for name, account in sorted(self.cfg.accounts.items())]
        else:
            result["accounts"] = self.settings.describe(self.settings_errors)
        return result

    def op_settings_save(self, request: dict[str, Any]) -> dict[str, Any]:
        name = str(request.get("account") or "")
        password = request.get("password")
        if password is not None and not isinstance(password, str):
            raise RequestError("password must be text")
        self._settings().save(name, request.get("settings") or {}, password)
        self.apply()
        return {"account": name}

    def op_settings_delete(self, request: dict[str, Any]) -> dict[str, Any]:
        name = str(request.get("account") or "")
        target = self._settings()
        socket_only = name not in target.base
        target.delete(name)
        self.apply()
        if socket_only:
            self.tokens.path(name).unlink(missing_ok=True)
        return {"account": name}

    def op_settings_reset(self, request: dict[str, Any]) -> dict[str, Any]:
        name = str(request.get("account") or "")
        self._settings().reset(name)
        self.apply()
        return {"account": name}

    def handle(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "the request must be a JSON object"}
        operation = getattr(self, f"op_{str(request.get('op', ''))}", None)
        if operation is None:
            return {"ok": False, "error": f"unknown operation {request.get('op')!r}"}
        try:
            result = operation(request)
        except (RequestError, MailError, send.SendError, auth.AuthError, auth.TransientAuthError, config.ConfigError) as error:
            return {"ok": False, "error": str(error)}
        except (KeyError, TypeError, ValueError) as error:
            return {"ok": False, "error": f"bad request: {error}"}
        except Exception as error:  # noqa: BLE001 - one bad request must not stop the service
            log.exception("request %s failed", request.get("op"))
            return {"ok": False, "error": f"internal error: {type(error).__name__}: {error}"}
        return {"ok": True, **result}


def legacy_daemon_running(path: Path = LEGACY_SOCKET) -> bool:
    """Whether a hermes-mail daemon answers on its socket. A socket that we may
    not open counts as running; a stale socket file does not."""
    if not path.is_socket():
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        try:
            probe.connect(str(path))
        except PermissionError:
            return True
        except OSError:
            return False
    return True


def lock_state(state_dir: Path):
    """Hold an exclusive lock on the state directory, so that a second daemon
    with the same state stops at once. The lock goes with the process."""
    state_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    handle = open(state_dir / "daemon.lock", "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise config.ConfigError(f"another uni-maild uses {state_dir}") from None
    return handle


def _write_new(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


class _Handler(socketserver.StreamRequestHandler):
    service: Service

    def handle(self) -> None:
        line = self.rfile.readline(REQUEST_LIMIT + 1)
        if len(line) > REQUEST_LIMIT or not line.endswith(b"\n"):
            response = {"ok": False, "error": "the request is too large or has no newline"}
        else:
            try:
                response = self.service.handle(json.loads(line))
            except ValueError:
                response = {"ok": False, "error": "the request is not JSON"}
        try:
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        except OSError:
            pass


class SocketServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, service: Service, mode: int = 0o660):
        if path.exists() or path.is_symlink():
            if not path.is_socket():
                raise SystemExit(f"{path} exists and is not a socket")
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = type("Handler", (_Handler,), {"service": service})
        old = os.umask(0o777 & ~mode)
        try:
            super().__init__(str(path), handler)
        finally:
            os.umask(old)
        self.path = path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uni-maild", description="uni-mail daemon: IMAP index, socket API and event log")
    parser.add_argument("--config", default=None, help="default: $UNI_MAIL_CONFIG, ~/.config/uni-mail/config.json when it exists, "
                                                        "or /etc/uni-mail/config.json")
    parser.add_argument("--log-level", default=os.environ.get("UNI_MAIL_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    args.config = args.config or config.default_config()
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr, format="%(levelname)s %(message)s")
    if sys.version_info < (3, 14):
        log.error("uni-maild needs Python 3.14 or later for IMAP IDLE")
        return 1
    if legacy_daemon_running() and os.environ.get("UNI_MAIL_ALLOW_LEGACY") != "1":
        log.error("a hermes-mail daemon is running (%s); stop and disable it first: see docs/MIGRATION.md", LEGACY_SOCKET)
        return 1
    try:
        cfg = config.load(args.config)
        lock = lock_state(cfg.state_dir)  # noqa: F841 - held until the process ends
        service = Service(cfg)
    except config.ConfigError as error:
        log.error("%s", error)
        return 1
    server = SocketServer(cfg.socket, service)

    def shutdown(*_: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    service.start()
    log.info("uni-maild: %d account(s), socket %s", len(service.accounts), cfg.socket)
    try:
        server.serve_forever()
    finally:
        service.stop()
        server.server_close()
        cfg.socket.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
