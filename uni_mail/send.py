"""Sending mail: address checks, message building and SMTP with XOAUTH2.

The daemon sends with the same OAuth refresh token that it uses for IMAP.
Google's sign-in scope (https://mail.google.com/) already allows SMTP.
Microsoft needs the extra scope SMTP.Send, which `uni-mail auth login
<account> --send` asks for. Gmail and Exchange Online both save a copy of a
message that was submitted over SMTP in the Sent folder, so the daemon does
not append one itself.
"""

from __future__ import annotations

import email
import email.policy
import json
import os
import re
import smtplib
import ssl
import threading
import time
from pathlib import Path
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses, make_msgid
from typing import Any, Callable

from . import auth, mime

MAX_SUBJECT = 998
MAX_BODY_CHARS = 500_000
MAX_RECIPIENTS = 50
MAX_NOTE = 1000
# The attachments of a forwarded message together. Gmail refuses a message over 25 MiB, and base64 adds a third.
MAX_FORWARD_ATTACHMENTS = 15 * 1024 * 1024
SMTP_TIMEOUT = 60
ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}")


class SendError(Exception):
    """A message was refused or could not be sent. The text is for the caller."""


def parse_addresses(value: Any, label: str) -> list[str]:
    """The bare addresses of a string or a list of strings. Display names are dropped.

    Anything that is not a plain ASCII address is an error, so that the headers
    and the SMTP envelope always hold exactly the addresses that were checked.
    """
    if value in (None, "", []):
        return []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise SendError(f"{label} must be an address or a list of addresses")
    found: list[str] = []
    for name, address in getaddresses(items, strict=True):
        if not ADDRESS.fullmatch(address):
            raise SendError(f"{label}: {(address or name)[:80]!r} is not a plain email address")
        if address.lower() not in (known.lower() for known in found):
            found.append(address)
    return found


def single_line(value: str, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise SendError(f"{label} must be text")
    if "\r" in value or "\n" in value or "\x00" in value:
        raise SendError(f"{label} must be one line")
    if len(value) > limit:
        raise SendError(f"{label} is longer than {limit} characters")
    return value.strip()


def reply_subject(original: str) -> str:
    return "Re: " + re.sub(r"^(?:re:\s*)+", "", original.strip(), flags=re.IGNORECASE)


def build_message(sender: str, to: list[str], cc: list[str], subject: str, body: str, html: bool = False,
                  in_reply_to: str = "") -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
    if in_reply_to:
        reference = single_line(in_reply_to, "the Message-ID of the original", 998)
        message["In-Reply-To"] = reference
        message["References"] = reference
    if html:
        message.set_content(mime.html_to_text(body) or " ")
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)
    return message


def forward_subject(original: str) -> str:
    """"Fwd: " and the original subject, with no second prefix when it already has one."""
    cleaned = " ".join(re.sub(r"^(?:\s*(?:fwd?|fw)\s*:)+", "", original or "", flags=re.IGNORECASE).split())
    return ("Fwd: " + (cleaned or "(no subject)"))[:MAX_SUBJECT]


def _line(value: str) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", value or "").split())


def _text_of(part: Message) -> str:
    payload = part.get_payload(decode=True)
    return mime.decode_text(payload if isinstance(payload, bytes) else b"", part.get_content_charset() or "")


def _attachment_data(part: Message) -> tuple[bytes, str, str]:
    """(data, maintype, subtype) of an attachment part."""
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        return (inner[0].as_bytes() if inner else b""), "message", "rfc822"
    payload = part.get_payload(decode=True)
    maintype, _, subtype = part.get_content_type().partition("/")
    if maintype == "multipart" or not subtype:
        maintype, subtype = "application", "octet-stream"
    return (payload if isinstance(payload, bytes) else b""), maintype, subtype


def forwarded_content(raw: bytes) -> tuple[str, list[Message]]:
    """The readable text and the attachment parts of a raw message. Nothing in it is run or fetched."""
    original = email.message_from_bytes(raw, policy=email.policy.compat32)
    plain = html = ""
    attached: list[Message] = []
    for part in original.walk():
        if part.is_multipart():
            continue
        kind = part.get_content_type()
        if part.get_content_disposition() == "attachment" or kind == "message/rfc822" or kind not in ("text/plain", "text/html") \
                or part.get_filename():
            attached.append(part)
        elif kind == "text/plain" and not plain:
            plain = _text_of(part)
        elif kind == "text/html" and not html:
            html = _text_of(part)
    return mime.choose_body(plain, mime.html_to_text(html) if html else ""), attached


def build_forward(sender: str, to: list[str], reply_to: list[str], origin: dict[str, str], text: str,
                  parts: list[Message], note: str = "", attachment_limit: int = MAX_FORWARD_ATTACHMENTS,
                  omitted_names: list[str] | None = None) -> tuple[EmailMessage, list[str]]:
    """A new message from `sender` that holds a forwarded one. Returns it and the names of the attachments left out.

    `origin` has account (the address of the original's account), sender, date and subject, as the index holds them.
    The first lines are what a phone notification shows, so they say what the message is and who wrote it.
    """
    omitted = list(omitted_names or [])
    kept: list[tuple[bytes, str, str, str]] = []
    total = 0
    for index, part in enumerate(parts, start=1):
        data, maintype, subtype = _attachment_data(part)
        name = _line(part.get_filename() or "") or f"attachment-{index}"
        name = name.replace("/", "_").replace("\\", "_")
        if total + len(data) > attachment_limit:
            omitted.append(f"{name} ({len(data) // 1024} KiB)")
            continue
        total += len(data)
        kept.append((data, maintype, subtype, name))
    lines = [f"Forwarded from {_line(origin['account'])}",
             f"From: {_line(origin['sender'])}", f"Date: {_line(origin['date'])}", f"Subject: {_line(origin['subject'])}"]
    if note:
        lines.append(f"Note: {_line(note)[:MAX_NOTE]}")
    if kept:
        lines.append(f"Attachments: {', '.join(item[3] for item in kept)}")
    if omitted:
        lines.append(f"Not forwarded, too large or not available ({len(omitted)}): {', '.join(omitted)}")
    body = text.strip() or "(the message has no text)"
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[the text was cut here]"
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    if reply_to:
        message["Reply-To"] = ", ".join(reply_to)
    message["Subject"] = forward_subject(origin["subject"])
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
    message.set_content("\n".join(lines) + "\n\n" + body + "\n")
    for data, maintype, subtype, name in kept:
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return message, omitted


class ForwardLedger:
    """Which mail was forwarded to whom, so that one mail never goes to the same address twice.

    One JSON line for each forwarded pair, in a file of the state directory. It holds IDs and addresses, no text.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def done(self, mail_id: str) -> set[str]:
        """The lower-case addresses that this mail was forwarded to."""
        found: set[str] = set()
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return found
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("mail_id") == mail_id:
                found.add(str(entry.get("to", "")).lower())
        return found

    def record(self, mail_id: str, addresses: list[str], account: str, message_id: str) -> None:
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a") as handle:
            for address in addresses:
                handle.write(json.dumps({"mail_id": mail_id, "to": address, "account": account, "message_id": message_id,
                                         "at": stamp}) + "\n")


def _xoauth2(user: str, token: str) -> Callable[..., str]:
    sent = False

    def respond(challenge: Any = None) -> str:
        nonlocal sent
        if sent:
            return ""  # the second challenge carries error details; an empty reply ends the exchange
        sent = True
        return f"user={user}\x01auth=Bearer {token}\x01\x01"

    return respond


def default_smtp_factory(host: str, port: int) -> smtplib.SMTP:
    return smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)


def connect(address: str, provider: auth.Provider, session: auth.OAuthSession, factory: Callable[[str, int], Any]) -> Any:
    """An SMTP connection, upgraded to TLS and signed in with XOAUTH2."""
    try:
        token = session.send_token()
    except auth.AuthError as error:
        if provider.send_scope:
            raise SendError(f"this sign-in cannot send mail yet ({error}). Sign in again with the extra scope: "
                            f"uni-mail auth login {session.account} --send") from None
        raise SendError(f"cannot send: {error}") from None
    except auth.TransientAuthError as error:
        raise SendError(f"cannot get an access token now: {error}") from None
    try:
        smtp = factory(provider.smtp_host, provider.smtp_port)
    except OSError as error:
        raise SendError(f"cannot reach {provider.smtp_host}:{provider.smtp_port}: {error}") from None
    try:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.auth("XOAUTH2", _xoauth2(address, token))
    except smtplib.SMTPAuthenticationError as error:
        _quit(smtp)
        hint = (" The tenant may have SMTP AUTH disabled, or the account lacks the SMTP.Send scope "
                f"(uni-mail auth login {session.account} --send)." if provider.send_scope else "")
        raise SendError(f"the SMTP server refused the sign-in ({error.smtp_code}).{hint}") from None
    except (smtplib.SMTPException, OSError) as error:
        _quit(smtp)
        raise SendError(f"SMTP connection to {provider.smtp_host} failed: {error}") from None
    return smtp


def _quit(smtp: Any) -> None:
    try:
        smtp.quit()
    except (smtplib.SMTPException, OSError):
        try:
            smtp.close()
        except OSError:
            pass


def deliver(address: str, provider: auth.Provider, session: auth.OAuthSession, message: EmailMessage, envelope: list[str],
            factory: Callable[[str, int], Any] = default_smtp_factory) -> dict[str, str]:
    """Submit the message to the recipients of `envelope`. Returns the addresses the server refused, with the reason."""
    smtp = connect(address, provider, session, factory)
    try:
        refused = smtp.send_message(message, from_addr=address, to_addrs=envelope)
    except smtplib.SMTPRecipientsRefused:
        raise SendError("the server refused every recipient") from None
    except (smtplib.SMTPException, OSError) as error:
        raise SendError(f"the server did not accept the message: {error}") from None
    finally:
        _quit(smtp)
    return {recipient: f"{code} {text.decode(errors='replace') if isinstance(text, bytes) else text}"
            for recipient, (code, text) in (refused or {}).items()}


def check(address: str, provider: auth.Provider, session: auth.OAuthSession,
          factory: Callable[[str, int], Any] = default_smtp_factory) -> None:
    """Sign in to the SMTP server and leave again. Sends nothing."""
    _quit(connect(address, provider, session, factory))
