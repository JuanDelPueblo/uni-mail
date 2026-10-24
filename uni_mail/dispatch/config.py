"""The "dispatch" section of the config file: where events go.

    "dispatch": {
      "state_dir": "/var/lib/uni-mail-dispatch",
      "retention_days": 30,
      "retry": {"max_attempts": 8, "initial_seconds": 30, "max_seconds": 3600},
      "destinations": {
        "nanobot": {"type": "command", "argv": ["/usr/bin/python3", "/opt/.../trigger.py"], "events": ["mail.new"]},
        "audit": {"type": "webhook", "url": "https://hooks.example.net/mail", "secret_file": "/etc/uni-mail/secrets/audit"}
      }
    }

The daemon ignores this section. The administrator owns it: no socket, MCP or
HTTP request can add or change a destination.
"""

from __future__ import annotations

import ipaddress
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ConfigError

DEFAULT_STATE_DIR = "/var/lib/uni-mail-dispatch"
NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KINDS = ("command", "webhook", "openclaw")
HTTP_KINDS = ("webhook", "openclaw")
# sysexits EX_USAGE and EX_DATAERR: the command says that a retry cannot help.
DEFAULT_PERMANENT_EXIT_CODES = (64, 65)


@dataclass(frozen=True)
class Retry:
    max_attempts: int = 8
    initial_seconds: float = 30.0
    max_seconds: float = 3600.0

    def delay(self, attempt: int) -> float:
        """The wait after failed attempt number `attempt` (1, 2, ...), before jitter."""
        return min(self.max_seconds, self.initial_seconds * 2 ** max(0, attempt - 1))


@dataclass(frozen=True)
class Destination:
    name: str
    kind: str
    events: tuple[str, ...] = ("mail.new",)
    accounts: tuple[str, ...] = ()
    enabled: bool = True
    timeout_seconds: float = 30.0
    retry: Retry = field(default_factory=Retry)
    # command
    argv: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    working_directory: str = ""
    permanent_exit_codes: tuple[int, ...] = DEFAULT_PERMANENT_EXIT_CODES
    # webhook
    url: str = ""
    secret_file: str = ""
    secret_env: str = ""
    allow_private_network: bool = False
    allow_http: bool = False
    # openclaw
    agent_id: str = ""
    session_key: str = ""

    def wants(self, event_type: str, account: str) -> bool:
        from ..events import matches

        return matches(self.events, event_type) and (not self.accounts or account in self.accounts)

    def secret(self) -> str:
        """The webhook secret, read at each use so that a rotation needs no restart."""
        if self.secret_file:
            try:
                return Path(self.secret_file).read_text().strip()
            except OSError as error:
                raise ConfigError(f"destination {self.name}: cannot read secret_file: {error.strerror or error}") from None
        return os.environ.get(self.secret_env, "").strip()


@dataclass(frozen=True)
class DispatchConfig:
    state_dir: Path = Path(DEFAULT_STATE_DIR)
    retention_days: int = 30
    retry: Retry = field(default_factory=Retry)
    destinations: dict[str, Destination] = field(default_factory=dict)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _strings(value: Any, what: str) -> tuple[str, ...]:
    _require(isinstance(value, list) and all(isinstance(item, str) and item for item in value), f"{what} must be a list of text")
    return tuple(value)


def parse_retry(raw: Any, base: Retry, what: str) -> Retry:
    if raw is None:
        return base
    _require(isinstance(raw, dict), f"{what} must be an object")
    retry = Retry(
        max_attempts=int(raw.get("max_attempts", base.max_attempts)),
        initial_seconds=float(raw.get("initial_seconds", base.initial_seconds)),
        max_seconds=float(raw.get("max_seconds", base.max_seconds)),
    )
    _require(1 <= retry.max_attempts <= 100, f"{what}.max_attempts must be 1 to 100")
    _require(1 <= retry.initial_seconds <= retry.max_seconds <= 7 * 86400,
             f"{what}: 1 <= initial_seconds <= max_seconds <= one week")
    return retry


def check_url(url: str, allow_http: bool) -> urllib.parse.SplitResult:
    """Check the form of a webhook URL. The addresses are checked at each send."""
    parts = urllib.parse.urlsplit(url)
    _require(parts.scheme in ("https", "http"), "the url must start with https:// or http://")
    _require(bool(parts.hostname), "the url has no host")
    _require(not parts.username and not parts.password, "the url must not hold a user name or password; use secret_file")
    _require(not parts.fragment, "the url must not have a fragment")
    if parts.scheme == "http" and not allow_http:
        try:
            loopback = ipaddress.ip_address(parts.hostname or "").is_loopback
        except ValueError:
            loopback = parts.hostname == "localhost"
        _require(loopback, "plain http is allowed only to a loopback address; use https or set allow_http")
    try:
        _ = parts.port
    except ValueError as error:
        raise ConfigError(f"the url has a bad port: {error}") from None
    return parts


def parse_destination(name: str, raw: Any, retry: Retry) -> Destination:
    what = f"dispatch.destinations.{name}"
    _require(bool(NAME.fullmatch(name)), f"destination name {name!r} must match {NAME.pattern}")
    _require(isinstance(raw, dict), f"{what} must be an object")
    kind = raw.get("type")
    _require(kind in KINDS, f"{what}.type must be one of {', '.join(KINDS)}")
    known = {"type", "events", "accounts", "enabled", "timeout_seconds", "retry"}
    known |= {"argv", "env", "working_directory", "permanent_exit_codes"} if kind == "command" else \
        {"url", "secret_file", "secret_env", "allow_private_network", "allow_http"}
    if kind == "openclaw":
        known |= {"agent_id", "session_key"}
    unknown = sorted(set(raw) - known)
    _require(not unknown, f"{what}: unknown key(s) {', '.join(unknown)} for a {kind} destination")
    timeout = float(raw.get("timeout_seconds", {"command": 60, "openclaw": 900}.get(kind, 15)))
    _require(1 <= timeout <= 3600, f"{what}.timeout_seconds must be 1 to 3600")
    common: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "events": _strings(raw.get("events", ["mail.new"]), f"{what}.events"),
        "accounts": _strings(raw.get("accounts", []), f"{what}.accounts") if raw.get("accounts") else (),
        "enabled": bool(raw.get("enabled", True)),
        "timeout_seconds": timeout,
        "retry": parse_retry(raw.get("retry"), retry, f"{what}.retry"),
    }
    _require(bool(common["events"]), f"{what}.events must not be empty")
    if kind == "command":
        argv = _strings(raw.get("argv"), f"{what}.argv")
        _require(bool(argv) and os.path.isabs(argv[0]), f"{what}.argv must start with an absolute program path")
        env = raw.get("env") or {}
        _require(isinstance(env, dict) and all(ENV_NAME.fullmatch(str(key)) and isinstance(value, str) for key, value in env.items()),
                 f"{what}.env must map variable names to text")
        codes = raw.get("permanent_exit_codes", list(DEFAULT_PERMANENT_EXIT_CODES))
        _require(isinstance(codes, list) and all(isinstance(code, int) and 0 < code < 256 for code in codes),
                 f"{what}.permanent_exit_codes must be a list of exit codes 1 to 255")
        directory = raw.get("working_directory") or ""
        _require(not directory or os.path.isabs(directory), f"{what}.working_directory must be an absolute path")
        return Destination(**common, argv=argv, env=dict(env), working_directory=directory, permanent_exit_codes=tuple(codes))
    url = str(raw.get("url") or "")
    allow_http = bool(raw.get("allow_http", False))
    try:
        check_url(url, allow_http)
    except ConfigError as error:
        raise ConfigError(f"{what}.url: {error}") from None
    secret_file, secret_env = str(raw.get("secret_file") or ""), str(raw.get("secret_env") or "")
    _require(bool(secret_file) != bool(secret_env), f"{what} needs exactly one of secret_file and secret_env")
    _require(not secret_file or os.path.isabs(secret_file), f"{what}.secret_file must be an absolute path")
    _require(not secret_env or bool(ENV_NAME.fullmatch(secret_env)), f"{what}.secret_env must be a variable name")
    extra: dict[str, Any] = {}
    if kind == "openclaw":
        agent = raw.get("agent_id")
        _require(isinstance(agent, str) and bool(agent.strip()), f"{what}.agent_id is required and must be text")
        session = raw.get("session_key", "")
        _require(isinstance(session, str), f"{what}.session_key must be text")
        extra = {"agent_id": agent.strip(), "session_key": session.strip()}
    return Destination(**common, url=url, secret_file=secret_file, secret_env=secret_env,
                       allow_private_network=bool(raw.get("allow_private_network", False)), allow_http=allow_http, **extra)


def parse(raw: Any) -> DispatchConfig:
    raw = raw or {}
    _require(isinstance(raw, dict), "dispatch must be an object")
    retry = parse_retry(raw.get("retry"), Retry(), "dispatch.retry")
    retention = int(raw.get("retention_days", 30))
    _require(1 <= retention <= 3650, "dispatch.retention_days must be 1 to 3650")
    destinations = raw.get("destinations") or {}
    _require(isinstance(destinations, dict), "dispatch.destinations must be an object")
    return DispatchConfig(
        state_dir=Path(raw.get("state_dir") or DEFAULT_STATE_DIR),
        retention_days=retention,
        retry=retry,
        destinations={name: parse_destination(name, value, retry) for name, value in destinations.items()},
    )
