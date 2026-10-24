"""Service configuration, read from a JSON file."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SOCKET = "/run/uni-mail/mail.sock"
PROVIDERS = ("microsoft", "google", "imap")
log = logging.getLogger("uni_mail")
DEFAULT_CONFIG = "/etc/uni-mail/config.json"
ACCOUNT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
# mail_archive moves a message here when the account sets no archive_folder.
# Gmail treats "[Gmail]/All Mail" as the archive: moving a message there just
# drops its Inbox label, since every message already lives in All Mail. The
# `imap` provider has no default; archiving needs an explicit archive_folder.
DEFAULT_ARCHIVE_FOLDER = {"microsoft": "Archive", "google": "[Gmail]/All Mail"}
# mail_trash moves a message here when the account sets no trash_folder. Both
# providers empty it after about 30 days, so a trashed message stays
# recoverable until then. The `imap` provider needs an explicit trash_folder.
DEFAULT_TRASH_FOLDER = {"microsoft": "Deleted Items", "google": "[Gmail]/Trash"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Account:
    name: str
    provider: str
    address: str
    auth: str = "oauth"
    host: str = ""
    port: int = 993
    password_file: str = ""
    folders: tuple[str, ...] = ("INBOX",)
    archive_folder: str = ""
    trash_folder: str = ""
    sync_days: int = 7
    poll_seconds: int = 300
    cache_limit_mb: int = 100
    max_part_mb: int = 25


@dataclass(frozen=True)
class Config:
    state_dir: Path
    socket: Path
    export_dir: Path
    extract_root: Path | None
    accounts: dict[str, Account]
    # The accounts as the config file holds them, before the web settings.
    base: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Accept account changes from the socket (`uni-mail account` and other clients).
    web_settings: bool = True
    # Days to keep the event log. 0 keeps it for ever.
    event_retention_days: int = 14


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def parse_account(name: str, raw: dict[str, Any]) -> Account:
    _require(bool(ACCOUNT_NAME.fullmatch(name)), f"account name {name!r} must match {ACCOUNT_NAME.pattern}")
    provider = raw.get("provider", "")
    _require(provider in PROVIDERS, f"account {name}: provider must be one of {', '.join(PROVIDERS)}")
    auth = raw.get("auth") or ("password" if provider == "imap" else "oauth")
    _require(auth in ("oauth", "password"), f"account {name}: auth must be oauth or password")
    _require(not (provider == "imap" and auth == "oauth"), f"account {name}: the imap provider supports only password sign-in")
    _require(bool(raw.get("address")), f"account {name}: address is required")
    if auth == "password":
        _require(bool(raw.get("password_file")), f"account {name}: password sign-in needs password_file")
    if provider == "imap":
        _require(bool(raw.get("host")), f"account {name}: the imap provider needs host")
    folders = tuple(raw.get("folders") or ("INBOX",))
    if raw.get("notify"):
        # hermes-mail notifications. Events now go to the destinations of the
        # "dispatch" section; see docs/MIGRATION.md.
        log.warning("account %s: notify was removed and is ignored; configure dispatch destinations instead", name)
    sync_days = int(raw.get("sync_days", 7))
    _require(1 <= sync_days <= 365, f"account {name}: sync_days must be 1 to 365")
    return Account(
        name=name,
        provider=provider,
        address=raw["address"],
        auth=auth,
        host=raw.get("host") or "",
        port=int(raw.get("port") or 993),
        password_file=raw.get("password_file") or "",
        folders=folders,
        archive_folder=raw.get("archive_folder") or DEFAULT_ARCHIVE_FOLDER.get(provider, ""),
        trash_folder=raw.get("trash_folder") or DEFAULT_TRASH_FOLDER.get(provider, ""),
        sync_days=sync_days,
        poll_seconds=max(30, int(raw.get("poll_seconds", 300))),
        cache_limit_mb=max(1, int(raw.get("cache_limit_mb", 100))),
        max_part_mb=max(1, int(raw.get("max_part_mb", 25))),
    )


def parse(raw: dict[str, Any]) -> Config:
    _require(bool(raw.get("state_dir")), "state_dir is required")
    base = {name: dict(value) for name, value in (raw.get("accounts") or {}).items()}
    accounts = {name: parse_account(name, value) for name, value in base.items()}
    state_dir = Path(raw["state_dir"])
    if "triage_retention_days" in raw:
        log.warning("triage_retention_days was removed and is ignored; the triage log is no longer written")
    retention = int(raw.get("event_retention_days", 14))
    _require(0 <= retention <= 3650, "event_retention_days must be 0 to 3650")
    return Config(
        state_dir=state_dir,
        socket=Path(raw.get("socket") or DEFAULT_SOCKET),
        export_dir=Path(raw.get("export_dir") or state_dir / "exports"),
        extract_root=Path(raw["extract_root"]) if raw.get("extract_root") else None,
        accounts=accounts,
        base=base,
        web_settings=bool(raw.get("web_settings", True)),
        event_retention_days=retention,
    )


def user_config() -> Path:
    """The config file of a user service: $XDG_CONFIG_HOME/uni-mail/config.json."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "uni-mail" / "config.json"


def default_config() -> str:
    """$UNI_MAIL_CONFIG, else the user's config file when it exists (not for root), else /etc/uni-mail/config.json."""
    configured = os.environ.get("UNI_MAIL_CONFIG", "")
    if configured:
        return configured
    if os.geteuid() != 0:
        try:
            if user_config().is_file():
                return str(user_config())
        except (OSError, RuntimeError):
            pass
    return DEFAULT_CONFIG


def configured_socket(path: str | Path) -> str:
    """The socket that a config file names, or "" when the file cannot be read."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return ""
    value = raw.get("socket") if isinstance(raw, dict) else None
    return value if isinstance(value, str) else ""


def resolve_socket(explicit: str = "", config_path: str | Path | None = None) -> str:
    """The socket of a client: the option, $UNI_MAIL_SOCKET, the config file's socket, /run/uni-mail/mail.sock."""
    return (explicit or os.environ.get("UNI_MAIL_SOCKET", "")
            or configured_socket(config_path if config_path is not None else default_config()) or DEFAULT_SOCKET)


def load(path: str | Path) -> Config:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    return parse(raw)
