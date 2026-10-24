"""Account changes over the socket (`uni-mail account ...`).

The config file holds the base accounts. A socket client changes them, and
the service keeps the changes in <state_dir>/settings.json (the same format
as hermes-mail):

- "accounts": the complete settings of each account that a client added or
  changed. They replace the base settings of that account.
- "removed": the names of base accounts that a client removed.

A password from a client goes into <state_dir>/passwords/<account> with mode
0600. The socket can never set a password file path or move an OAuth account
to another host. Thus a socket user cannot send a stored password or token to
a different server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import ACCOUNT_NAME, Account, ConfigError, parse_account

EDITABLE = (
    "provider", "address", "auth", "host", "port", "folders", "archive_folder", "trash_folder",
    "sync_days", "poll_seconds", "cache_limit_mb", "max_part_mb",
)
# A password stays valid only for the same server and user name.
PASSWORD_SCOPE = ("provider", "address", "host", "port")


def _same(raw: dict[str, Any], base: dict[str, Any], key: str) -> bool:
    if key == "port":
        return int(raw.get("port") or 993) == int(base.get("port") or 993)
    return (raw.get(key) or "") == (base.get(key) or "")


def form(account: Account) -> dict[str, Any]:
    """The editable settings of an account, in the socket format."""
    return {
        "provider": account.provider,
        "address": account.address,
        "auth": account.auth,
        "host": account.host,
        "port": account.port,
        "folders": list(account.folders),
        "archive_folder": account.archive_folder,
        "trash_folder": account.trash_folder,
        "sync_days": account.sync_days,
        "poll_seconds": account.poll_seconds,
        "cache_limit_mb": account.cache_limit_mb,
        "max_part_mb": account.max_part_mb,
    }


class Settings:
    def __init__(self, state_dir: Path, base: dict[str, dict[str, Any]]):
        self.path = state_dir / "settings.json"
        self.passwords = state_dir / "passwords"
        self.base = base
        self.changed: dict[str, dict[str, Any]] = {}
        self.removed: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            raise ConfigError(f"cannot read {self.path}: {error}") from error
        accounts = raw.get("accounts") if isinstance(raw, dict) else None
        removed = raw.get("removed") if isinstance(raw, dict) else None
        self.changed = {str(name): value for name, value in (accounts or {}).items() if isinstance(value, dict)}
        self.removed = {str(name) for name in (removed or []) if str(name) in self.base}

    def _save(self) -> None:
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump({"accounts": self.changed, "removed": sorted(self.removed)}, handle, indent=1, sort_keys=True)
        os.replace(temporary, self.path)

    # Passwords

    def password_path(self, name: str) -> Path:
        return self.passwords / name

    def has_password(self, name: str) -> bool:
        return self.password_path(name).is_file()

    def _write_password(self, name: str, password: str) -> None:
        self.passwords.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.password_path(name)
        temporary = target.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(password)
        os.replace(temporary, target)

    def _drop_password(self, name: str) -> None:
        self.password_path(name).unlink(missing_ok=True)

    # Effective settings

    def _raw(self, name: str, changed: dict[str, Any], password: bool) -> dict[str, Any]:
        """The service settings of a changed account: the client values and the
        right password file."""
        base = self.base.get(name) or {}
        raw = {key: changed[key] for key in EDITABLE if key in changed}
        if password:
            raw["password_file"] = str(self.password_path(name))
        elif base.get("password_file") and all(_same(raw, base, key) for key in PASSWORD_SCOPE):
            raw["password_file"] = base["password_file"]
        return raw

    def names(self) -> list[str]:
        return sorted((set(self.base) - self.removed) | set(self.changed))

    def raw(self, name: str) -> dict[str, Any] | None:
        if name in self.changed:
            return self._raw(name, self.changed[name], self.has_password(name))
        if name in self.removed:
            return None
        return self.base.get(name)

    def accounts(self) -> tuple[dict[str, Account], dict[str, str]]:
        """The accounts to run, and an error for each account that is not valid."""
        accounts: dict[str, Account] = {}
        errors: dict[str, str] = {}
        for name in self.names():
            raw = self.raw(name)
            if raw is None:
                continue
            try:
                accounts[name] = parse_account(name, raw)
            except (ConfigError, TypeError, ValueError) as error:
                errors[name] = str(error)
        return accounts, errors

    # Changes

    def validate(self, name: str, values: dict[str, Any], password: str | None) -> Account:
        if not isinstance(values, dict):
            raise ConfigError("the account settings must be a JSON object")
        if not ACCOUNT_NAME.fullmatch(name):
            raise ConfigError(f"account name {name!r} must match {ACCOUNT_NAME.pattern}")
        if "password_file" in values:
            raise ConfigError("a socket client cannot set a password file; send the password")
        unknown = sorted(set(values) - set(EDITABLE))
        if unknown:
            raise ConfigError(f"unknown account settings: {', '.join(unknown)}")
        folders = values.get("folders")
        if folders is not None and (
            not isinstance(folders, list) or not folders or not all(isinstance(item, str) and item.strip() for item in folders)
        ):
            raise ConfigError("folders must be a list of folder names")
        for key, low, high in (("port", 1, 65535), ("poll_seconds", 30, 1500), ("cache_limit_mb", 1, 1 << 20), ("max_part_mb", 1, 1 << 20)):
            if values.get(key) is not None and not low <= int(values[key]) <= high:
                raise ConfigError(f"{key} must be {low} to {high}")
        if any(char.isspace() for char in str(values.get("host") or "")):
            raise ConfigError("host must not contain spaces")
        if password is not None and not password.strip():
            raise ConfigError("the password is empty")
        raw = self._raw(name, values, password is not None or self.has_password(name))
        account = parse_account(name, raw)
        base = self.base.get(name) or {}
        if account.auth == "oauth" and not _same(raw, base, "host"):
            raise ConfigError("an OAuth account always uses the host of its provider; leave host empty")
        return account

    def save(self, name: str, values: dict[str, Any], password: str | None = None) -> Account:
        account = self.validate(name, values, password)
        if password is not None:
            self._write_password(name, password.strip())
        elif account.auth == "oauth":
            self._drop_password(name)
        self.changed[name] = {key: values[key] for key in EDITABLE if key in values}
        self.removed.discard(name)
        self._save()
        return account

    def delete(self, name: str) -> None:
        if name not in self.changed and (name not in self.base or name in self.removed):
            raise ConfigError(f"unknown account {name!r}")
        self.changed.pop(name, None)
        if name in self.base:
            self.removed.add(name)
        self._drop_password(name)
        self._save()

    def reset(self, name: str) -> None:
        if name not in self.base:
            raise ConfigError(f"the account {name} is not in the base config; remove it instead")
        self.changed.pop(name, None)
        self.removed.discard(name)
        self._drop_password(name)
        self._save()

    def describe(self, errors: dict[str, str]) -> list[dict[str, Any]]:
        """Every account, with the removed base accounts."""
        result = []
        for name in sorted(set(self.base) | set(self.changed)):
            base = None
            if name in self.base:
                try:
                    base = form(parse_account(name, self.base[name]))
                except (ConfigError, TypeError, ValueError):
                    base = {key: self.base[name].get(key) for key in EDITABLE}
            raw = self.raw(name)
            current = base
            if raw is not None:
                try:
                    current = form(parse_account(name, raw))
                except (ConfigError, TypeError, ValueError):
                    current = {key: raw.get(key) for key in EDITABLE}
            password = "socket" if self.has_password(name) else "base" if raw and raw.get("password_file") else ""
            result.append({
                "name": name,
                "source": "base" if name in self.base else "socket",
                "changed": name in self.changed and name in self.base,
                "removed": name in self.removed,
                "settings": current,
                "base": base,
                "password": password,
                "error": errors.get(name, ""),
            })
        return result
