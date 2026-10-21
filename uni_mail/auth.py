"""OAuth2 sign-in and token refresh for IMAP XOAUTH2.

The service uses the public OAuth clients of Thunderbird. The operator signs
in one time in a browser and pastes the redirect URL. The service keeps the
refresh token in its state directory with mode 0600 and never returns a token
on the socket.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Provider:
    host: str
    port: int
    auth_endpoint: str
    token_endpoint: str
    client_id: str
    redirect_uri: str
    scope: str
    # Thunderbird ships the Google secret in its public source code. Google
    # treats the secret of an installed application as public, so it does not
    # protect anything and is not a credential of this project.
    client_secret: str | None = None
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    # Sending mail: SMTP with XOAUTH2. An empty send_scope means that the
    # sign-in scope already allows sending (Google's full mail scope).
    smtp_host: str = ""
    smtp_port: int = 587
    send_scope: str = ""


# The built-in Thunderbird OAuth issuers from
# comm/mailnews/base/src/OAuth2Providers.sys.mjs. The "common" Microsoft
# endpoint accepts work or school accounts and personal accounts.
PROVIDERS: dict[str, Provider] = {
    "microsoft": Provider(
        host="outlook.office365.com",
        port=993,
        auth_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        client_id="9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        redirect_uri="https://localhost",
        scope="https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        smtp_host="smtp.office365.com",
        send_scope="https://outlook.office.com/SMTP.Send",
    ),
    "google": Provider(
        host="imap.gmail.com",
        port=993,
        auth_endpoint="https://accounts.google.com/o/oauth2/auth",
        token_endpoint="https://www.googleapis.com/oauth2/v3/token",
        client_id="406964657835-aq8lmia8j95dhl1a2bvharmfk3t1hgqj.apps.googleusercontent.com",
        client_secret="kSmqreRr0qwBWJgbf5Y-PjSU",
        redirect_uri="http://localhost",
        scope="https://mail.google.com/",
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        smtp_host="smtp.gmail.com",
    ),
}

AADSTS_HINTS = {
    "50011": "The redirect URI does not match the app registration.",
    "50076": "The tenant asks for MFA again. Sign in again.",
    "50079": "The tenant asks for MFA enrollment.",
    "50105": "The tenant allows this app only for assigned users.",
    "53003": "A Conditional Access policy blocks this sign-in.",
    "65001": "The tenant has not given consent to this app for your account.",
    "70000": "The authorization code is not valid or was already used.",
    "70008": "The refresh token expired. Sign in again.",
    "700082": "The refresh token expired because it was not used. Sign in again.",
    "700016": "The app does not exist in the tenant.",
    "90094": "The tenant requires admin consent for this app.",
}

# Token endpoint errors that a retry cannot fix. The account then waits for a
# new sign-in.
PERMANENT_ERRORS = {"invalid_grant", "interaction_required", "consent_required", "invalid_client", "unauthorized_client"}
LOGIN_TIMEOUT = 15 * 60


class AuthError(Exception):
    """The account needs a new sign-in."""


class TransientAuthError(Exception):
    """The token endpoint is not reachable or failed for a time."""


def error_text(payload: dict[str, Any]) -> str:
    error = str(payload.get("error", "unknown_error"))
    lines = str(payload.get("error_description", "")).splitlines()
    text = f"{error}: {lines[0]}" if lines and lines[0] else error
    match = re.search(r"AADSTS(\d+)", text)
    if match and match.group(1) in AADSTS_HINTS:
        text += f" ({AADSTS_HINTS[match.group(1)]})"
    return text


def token_request(provider: Provider, fields: dict[str, str], timeout: float = 30) -> dict[str, Any]:
    data = {"client_id": provider.client_id, **fields}
    if provider.client_secret:
        data["client_secret"] = provider.client_secret
    request = urllib.request.Request(
        provider.token_endpoint,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            with error:
                payload = json.loads(error.read())
        except ValueError:
            raise TransientAuthError(f"HTTP {error.code} from the token endpoint") from None
        if payload.get("error") in PERMANENT_ERRORS:
            raise AuthError(error_text(payload)) from None
        raise TransientAuthError(error_text(payload)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise TransientAuthError(f"cannot reach the token endpoint: {getattr(error, 'reason', error)}") from None
    except ValueError:
        raise TransientAuthError("the token endpoint did not return JSON") from None
    if "access_token" not in payload:
        raise TransientAuthError("the token response has no access token")
    return payload


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(provider: Provider, user: str, state: str, challenge: str, send: bool = False) -> str:
    scope = provider.scope
    if send and provider.send_scope:
        # Keep the existing scopes and add the one for sending.
        scope = f"{provider.send_scope} {scope}"
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "scope": scope,
        "state": state,
        "login_hint": user,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **provider.extra_auth_params,
    }
    return f"{provider.auth_endpoint}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"


def code_from_redirect(text: str, state: str) -> str:
    text = text.strip()
    if not text:
        raise AuthError("no redirect URL was given")
    query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("?")
    params = urllib.parse.parse_qs(query)
    if "error" in params:
        raise AuthError(error_text({"error": params["error"][0], "error_description": params.get("error_description", [""])[0]}))
    if "code" not in params:
        raise AuthError("the redirect URL has no code parameter; paste the full URL from the address bar")
    if params.get("state", [None])[0] != state:
        raise AuthError("the state value in the redirect URL does not match this sign-in")
    return params["code"][0]


def xoauth2(user: str, token: str):
    """Return an imaplib authenticator for XOAUTH2."""
    sent = False

    def respond(challenge: bytes) -> bytes:
        nonlocal sent
        # A second challenge carries the error details. An empty reply ends
        # the exchange, so the server can send its NO response.
        if sent:
            return b""
        sent = True
        return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()

    return respond


class TokenStore:
    """Refresh tokens, one file for each account, mode 0600."""

    def __init__(self, directory: Path):
        self.directory = directory

    def path(self, account: str) -> Path:
        return self.directory / f"{account}.json"

    def load(self, account: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.path(account).read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise AuthError(f"cannot read the token file: {error}") from error

    def save(self, account: str, value: dict[str, Any]) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.path(account)
        temporary = target.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle)
        os.replace(temporary, target)

    def generation(self, account: str) -> float:
        try:
            return self.path(account).stat().st_mtime
        except FileNotFoundError:
            return 0.0


class OAuthSession:
    """Access tokens for one account. Thread-safe."""

    def __init__(self, account: str, provider: Provider, store: TokenStore):
        self.account = account
        self.provider = provider
        self.store = store
        self._lock = threading.Lock()
        # Access tokens by scope; the empty scope is the sign-in scope (IMAP).
        self._tokens: dict[str, tuple[str, float]] = {}

    def access_token(self, scope: str = "") -> str:
        """An access token. A non-empty scope asks the token endpoint for exactly that scope."""
        with self._lock:
            cached = self._tokens.get(scope)
            if cached and time.time() < cached[1] - 120:
                return cached[0]
            saved = self.store.load(self.account)
            if not saved or not saved.get("refresh_token"):
                raise AuthError("no sign-in; run: uni-mail auth login " + self.account)
            fields = {"grant_type": "refresh_token", "refresh_token": saved["refresh_token"]}
            if scope:
                fields["scope"] = f"{scope} offline_access"
            tokens = token_request(self.provider, fields)
            if tokens.get("refresh_token") and tokens["refresh_token"] != saved["refresh_token"]:
                self.store.save(self.account, {**saved, "refresh_token": tokens["refresh_token"], "refreshed": time.time()})
            self._tokens[scope] = (tokens["access_token"], time.time() + int(tokens.get("expires_in", 3600)))
            return tokens["access_token"]

    def send_token(self) -> str:
        """An access token that allows sending through SMTP. Raises AuthError when the sign-in lacks the scope."""
        return self.access_token(self.provider.send_scope)

    def forget(self) -> None:
        with self._lock:
            self._tokens.clear()


@dataclass
class PendingLogin:
    account: str
    state: str
    verifier: str
    created: float
    send: bool = False


class LoginFlow:
    """Browser sign-in: begin gives a URL, finish takes the redirect URL."""

    def __init__(self, store: TokenStore):
        self.store = store
        self._pending: dict[str, PendingLogin] = {}
        self._lock = threading.Lock()

    def begin(self, account: str, provider: Provider, user: str, send: bool = False) -> dict[str, str]:
        state = secrets.token_urlsafe(16)
        verifier, challenge = pkce_pair()
        with self._lock:
            now = time.time()
            self._pending = {key: value for key, value in self._pending.items() if now - value.created < LOGIN_TIMEOUT}
            self._pending[account] = PendingLogin(account, state, verifier, now, send)
        return {
            "url": authorization_url(provider, user, state, challenge, send),
            "redirect_uri": provider.redirect_uri,
        }

    def finish(self, account: str, provider: Provider, redirect: str) -> None:
        with self._lock:
            pending = self._pending.get(account)
            if not pending or time.time() - pending.created > LOGIN_TIMEOUT:
                raise AuthError("no sign-in is in progress for this account; start again")
        code = code_from_redirect(redirect, pending.state)
        tokens = token_request(provider, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": provider.redirect_uri,
            "code_verifier": pending.verifier,
        })
        if not tokens.get("refresh_token"):
            raise AuthError("the provider gave no refresh token (offline access denied)")
        self.store.save(account, {"refresh_token": tokens["refresh_token"], "obtained": time.time()})
        with self._lock:
            self._pending.pop(account, None)
