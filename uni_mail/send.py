"""Sending mail: address checks, message building and SMTP with XOAUTH2.

The daemon sends with the same OAuth refresh token that it uses for IMAP.
Google's sign-in scope (https://mail.google.com/) already allows SMTP.
Microsoft needs the extra scope SMTP.Send, which `uni-mail auth login
<account> --send` asks for. Gmail and Exchange Online both save a copy of a
message that was submitted over SMTP in the Sent folder, so the daemon does
not append one itself.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from typing import Any, Callable

from . import auth, mime

MAX_SUBJECT = 998
MAX_BODY_CHARS = 500_000
MAX_RECIPIENTS = 50
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
