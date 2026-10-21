"""Socket client for uni-maild. The CLI, the dispatcher and the MCP server use it.

It uses only the standard library, so that other programs can copy it.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Dict, List, Optional

DEFAULT_SOCKET = "/run/uni-mail/mail.sock"
RESPONSE_LIMIT = 64 * 1024 * 1024


class MailServiceError(Exception):
    """The service is not reachable or it refused the request."""


def socket_path(configured: str = "") -> str:
    return configured or os.environ.get("UNI_MAIL_SOCKET", "") or DEFAULT_SOCKET


class Client:
    def __init__(self, path: str = "", timeout: float = 120.0):
        self.path = socket_path(path)
        self.timeout = timeout

    def request(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout or self.timeout)
            try:
                connection.connect(self.path)
            except (FileNotFoundError, ConnectionRefusedError) as error:
                raise MailServiceError(f"the mail service is not running (no listener on {self.path})") from error
            except PermissionError as error:
                raise MailServiceError(f"no permission to use the mail service socket {self.path}") from error
            try:
                connection.sendall(json.dumps(payload).encode() + b"\n")
                chunks: List[bytes] = []
                total = 0
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > RESPONSE_LIMIT:
                        raise MailServiceError("the mail service response is too large")
                    if chunk.endswith(b"\n"):
                        break
            except socket.timeout as error:
                raise MailServiceError(f"the mail service did not answer in {timeout or self.timeout:.0f} s") from error
        if not chunks:
            raise MailServiceError("the mail service closed the connection without an answer")
        try:
            response = json.loads(b"".join(chunks))
        except ValueError as error:
            raise MailServiceError("the mail service sent a response that is not JSON") from error
        if not response.get("ok"):
            error = MailServiceError(response.get("error") or "the mail request failed")
            error.response = response  # type: ignore[attr-defined]
            raise error
        return response

    # One method for each operation.

    def status(self) -> Dict[str, Any]:
        return self.request({"op": "status"})

    def accounts(self) -> Dict[str, Any]:
        return self.request({"op": "accounts"})["accounts"]

    def folders(self, account: str) -> Dict[str, Any]:
        return self.request({"op": "folders", "account": account})

    def list(self, **filters: Any) -> Dict[str, Any]:
        return self.request({"op": "list", **{key: value for key, value in filters.items() if value not in (None, "", False)}})

    def show(self, mail_id: str, full: bool = True) -> Dict[str, Any]:
        return self.request({"op": "show", "id": mail_id, "full": full})

    def attachments(self, mail_id: str) -> Dict[str, Any]:
        return self.request({"op": "attachments", "id": mail_id})

    def export_attachment(self, mail_id: str, index: int) -> Dict[str, Any]:
        return self.request({"op": "export_attachment", "id": mail_id, "index": index})

    def extract_attachment(self, mail_id: str, index: int, output: str) -> Dict[str, Any]:
        return self.request({"op": "extract_attachment", "id": mail_id, "index": index, "output": output})

    def mark(self, mail_ids: List[str], read: bool) -> Dict[str, Any]:
        return self.request({"op": "mark", "ids": list(mail_ids), "read": read})

    def archive(self, mail_ids: List[str]) -> Dict[str, Any]:
        return self.request({"op": "archive", "ids": list(mail_ids)})

    def events(self, after: int = 0, timeout: float = 0, limit: int = 100) -> Dict[str, Any]:
        """The events after the sequence number `after`, and the head of the log."""
        return self.request({"op": "events", "after": after, "timeout": timeout, "limit": limit}, timeout=timeout + 30)

    def event_head(self) -> Dict[str, Any]:
        return self.request({"op": "event_head"})

    def events_consumed(self, upto: int) -> int:
        return self.request({"op": "events_consumed", "upto": int(upto)})["consumed"]

    def auth_begin(self, account: str, send: bool = False) -> Dict[str, Any]:
        return self.request({"op": "auth_begin", "account": account, **({"send": True} if send else {})})

    def send(self, account: str, to: List[str], subject: str, body: str, cc: Optional[List[str]] = None,
             bcc: Optional[List[str]] = None, html: bool = False, reply_to: str = "") -> Dict[str, Any]:
        """Send a message from an account. Sending can take a while, so the timeout is longer."""
        payload: Dict[str, Any] = {"op": "send", "account": account, "to": list(to), "subject": subject, "body": body}
        for key, value in (("cc", cc), ("bcc", bcc)):
            if value:
                payload[key] = list(value)
        if html:
            payload["html"] = True
        if reply_to:
            payload["reply_to"] = reply_to
        return self.request(payload, timeout=180)

    def send_check(self, account: str) -> Dict[str, Any]:
        return self.request({"op": "send_check", "account": account}, timeout=120)

    def auth_finish(self, account: str, redirect: str) -> Dict[str, Any]:
        return self.request({"op": "auth_finish", "account": account, "redirect": redirect})

    def settings(self) -> Dict[str, Any]:
        return self.request({"op": "settings"})

    def settings_save(self, account: str, settings: Dict[str, Any], password: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"op": "settings_save", "account": account, "settings": settings}
        if password is not None:
            payload["password"] = password
        return self.request(payload)

    def settings_delete(self, account: str) -> Dict[str, Any]:
        return self.request({"op": "settings_delete", "account": account})

    def settings_reset(self, account: str) -> Dict[str, Any]:
        return self.request({"op": "settings_reset", "account": account})
