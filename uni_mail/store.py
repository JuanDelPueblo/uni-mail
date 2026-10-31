"""SQLite index of recent mail, the event log and the part cache."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    account TEXT NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    baseline INTEGER NOT NULL DEFAULT 0,
    synced REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account, folder)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    recipients TEXT NOT NULL DEFAULT '',
    cc TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    read INTEGER NOT NULL DEFAULT 0,
    flagged INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    parts TEXT NOT NULL DEFAULT '[]',
    preview TEXT NOT NULL DEFAULT '',
    indexed REAL NOT NULL,
    UNIQUE (account, folder, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS messages_date ON messages (date);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    account TEXT NOT NULL,
    mail_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    event_id TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS notes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    mail_id TEXT NOT NULL,
    account TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_mail ON notes (mail_id);
CREATE INDEX IF NOT EXISTS notes_created ON notes (created);
"""
# PRAGMA user_version of the index. 0 is the hermes-mail format; 2 adds the
# stable event IDs and the event data. The upgrade only adds columns, so the
# old code can still read a database that this version upgraded.
SCHEMA_VERSION = 2

# Index columns that list and search return. The parts and the preview stay
# out of lists to keep them small.
SUMMARY = ("id", "account", "folder", "message_id", "date", "sender", "recipients", "cc", "subject", "read", "flagged", "size")


def mail_id(account: str, folder: str, uidvalidity: int, uid: int) -> str:
    digest = hashlib.sha256(f"{account}\0{folder}\0{uidvalidity}\0{uid}".encode()).hexdigest()[:16]
    return f"{account}.{digest}"


class Store:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cache_dir = state_dir / "cache"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(state_dir / "index.sqlite3", check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._upgrade()
        self.events_changed = threading.Condition(self._lock)

    def _upgrade(self) -> None:
        with self._lock:
            if self._db.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
                # hermes-mail may have run in between (a rollback) and appended events without an ID.
                if self._db.execute("SELECT 1 FROM events WHERE event_id IS NULL LIMIT 1").fetchone() is None:
                    return
            self._db.execute("BEGIN IMMEDIATE")
            try:
                columns = {row["name"] for row in self._db.execute("PRAGMA table_info(events)")}
                if "event_id" not in columns:
                    self._db.execute("ALTER TABLE events ADD COLUMN event_id TEXT")
                if "data" not in columns:
                    self._db.execute("ALTER TABLE events ADD COLUMN data TEXT NOT NULL DEFAULT '{}'")
                # Events from hermes-mail get an ID once; it stays the same afterwards.
                for row in self._db.execute("SELECT seq FROM events WHERE event_id IS NULL").fetchall():
                    self._db.execute("UPDATE events SET event_id=? WHERE seq=?", (str(uuid.uuid4()), row["seq"]))
                self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_event_id ON events (event_id)")
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    def _write(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            cursor = self._db.execute(sql, tuple(args))
            return cursor.rowcount

    # Folders

    def folder(self, account: str, folder: str) -> sqlite3.Row | None:
        rows = self._rows("SELECT * FROM folders WHERE account=? AND folder=?", (account, folder))
        return rows[0] if rows else None

    def reset_folder(self, account: str, folder: str, uidvalidity: int) -> None:
        """Start a folder again, for example after a UIDVALIDITY change. The
        next sync is a new baseline, so it sends no events."""
        with self._lock:
            for row in self._rows("SELECT id FROM messages WHERE account=? AND folder=?", (account, folder)):
                self.drop_cache(row["id"])
            self._write("DELETE FROM messages WHERE account=? AND folder=?", (account, folder))
            self._write(
                "INSERT INTO folders (account, folder, uidvalidity, baseline, synced) VALUES (?, ?, ?, 0, 0) "
                "ON CONFLICT (account, folder) DO UPDATE SET uidvalidity=excluded.uidvalidity, baseline=0, synced=0",
                (account, folder, uidvalidity),
            )

    def finish_sync(self, account: str, folder: str) -> None:
        self._write("UPDATE folders SET baseline=1, synced=? WHERE account=? AND folder=?", (time.time(), account, folder))

    def drop_other_folders(self, account: str, folders: Iterable[str]) -> None:
        keep = set(folders)
        for row in self._rows("SELECT DISTINCT folder FROM folders WHERE account=?", (account,)):
            if row["folder"] not in keep:
                self.reset_folder(account, row["folder"], 0)
                self._write("DELETE FROM folders WHERE account=? AND folder=?", (account, row["folder"]))

    def drop_other_accounts(self, accounts: Iterable[str]) -> None:
        keep = set(accounts)
        for row in self._rows("SELECT DISTINCT account FROM folders"):
            if row["account"] not in keep:
                self.drop_other_folders(row["account"], ())

    # Messages

    def uids(self, account: str, folder: str) -> dict[int, str]:
        rows = self._rows("SELECT uid, id FROM messages WHERE account=? AND folder=?", (account, folder))
        return {row["uid"]: row["id"] for row in rows}

    def add(self, record: dict[str, Any], event: tuple[str, dict[str, Any]] | None = None) -> None:
        """Index a message. With `event` (kind, data), record that event in the
        same transaction, so a crash cannot index the mail without its event."""
        with self.events_changed:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO messages (id, account, folder, uidvalidity, uid, message_id, date, sender, recipients, cc, "
                    "subject, read, flagged, size, parts, preview, indexed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["id"], record["account"], record["folder"], record["uidvalidity"], record["uid"],
                        record.get("message_id", ""), record.get("date", ""), record.get("sender", ""),
                        record.get("recipients", ""), record.get("cc", ""), record.get("subject", ""),
                        int(bool(record.get("read"))), int(bool(record.get("flagged"))), int(record.get("size", 0)),
                        json.dumps(record.get("parts", [])), record.get("preview", ""), time.time(),
                    ),
                )
                if event is not None:
                    self._insert_event(event[0], record["account"], record["id"], "", event[1])
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            if event is not None:
                self.events_changed.notify_all()

    def remove(self, ids: Iterable[str]) -> None:
        with self._lock:
            for identifier in ids:
                self._write("DELETE FROM messages WHERE id=?", (identifier,))
                self.drop_cache(identifier)

    def set_flags(self, identifier: str, read: bool, flagged: bool) -> None:
        self._write("UPDATE messages SET read=?, flagged=? WHERE id=?", (int(read), int(flagged), identifier))

    def get(self, identifier: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM messages WHERE id=?", (identifier,))
        if not rows:
            return None
        record = dict(rows[0])
        record["parts"] = json.loads(record["parts"])
        record["read"] = bool(record["read"])
        record["flagged"] = bool(record["flagged"])
        return record

    def query(
        self, *, account: str = "", since: str = "", until: str = "", unread: bool = False, text: str = "", limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses, args = [], []
        if account:
            clauses.append("account=?")
            args.append(account)
        if since:
            clauses.append("date>=?")
            args.append(since)
        if until:
            clauses.append("date<=?")
            args.append(until)
        if unread:
            clauses.append("read=0")
        if text:
            pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("(sender LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\' OR preview LIKE ? ESCAPE '\\')")
            args += [pattern, pattern, pattern]
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._rows(f"SELECT {', '.join(SUMMARY)} FROM messages {where} ORDER BY date DESC LIMIT ?", (*args, max(1, min(limit, 500))))
        result = []
        for row in rows:
            record = dict(row)
            record["read"] = bool(record["read"])
            record["flagged"] = bool(record["flagged"])
            result.append(record)
        return result

    def counts(self, account: str) -> dict[str, int]:
        row = self._rows("SELECT COUNT(*) AS total, SUM(read=0) AS unread FROM messages WHERE account=?", (account,))[0]
        return {"messages": row["total"] or 0, "unread": row["unread"] or 0}

    def last_sync(self, account: str) -> float:
        row = self._rows("SELECT MIN(synced) AS synced FROM folders WHERE account=?", (account,))[0]
        return row["synced"] or 0.0

    # Events. The log is append-only. `done` means that the dispatcher has
    # stored the event (it was "acknowledged" in hermes-mail); the delivery
    # state of each destination lives in the dispatcher's own database.

    def _insert_event(self, kind: str, account: str, mail_id: str, detail: str, data: dict[str, Any] | None) -> int:
        cursor = self._db.execute(
            "INSERT INTO events (kind, account, mail_id, detail, created, event_id, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, account, mail_id, detail, time.time(), str(uuid.uuid4()), json.dumps(data or {}, ensure_ascii=False)),
        )
        return cursor.lastrowid

    def add_event(self, kind: str, account: str, mail_id: str = "", detail: str = "",
                  data: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.events_changed:
            seq = self._insert_event(kind, account, mail_id, detail, data)
            self.events_changed.notify_all()
            return self.event(seq)

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["data"] = json.loads(record["data"] or "{}")
        return record

    def event(self, seq: int) -> dict[str, Any]:
        return self._event_row(self._rows("SELECT * FROM events WHERE seq=?", (seq,))[0])

    def events_after(self, after: int, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, max(1, min(limit, 1000))))
        return [self._event_row(row) for row in rows]

    def wait_events(self, after: int, timeout: float, limit: int = 100) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        with self.events_changed:
            while True:
                events = self.events_after(after, limit)
                remaining = deadline - time.monotonic()
                if events or remaining <= 0:
                    return events
                self.events_changed.wait(remaining)

    def event_head(self) -> dict[str, int | None]:
        """The first and last sequence numbers, and the first event that no
        dispatcher has stored yet (None when there is none)."""
        row = self._rows("SELECT MIN(seq) AS first, MAX(seq) AS last, MIN(CASE WHEN done=0 THEN seq END) AS pending FROM events")[0]
        return {"first": row["first"], "last": row["last"], "first_pending": row["pending"]}

    def consume_events(self, upto: int) -> int:
        return self._write("UPDATE events SET done=1 WHERE seq<=? AND done=0", (int(upto),))

    def prune_events(self, days: int) -> int:
        """Delete the events that are older than `days`. 0 keeps every event.
        The newest event always stays, so that the sequence never restarts."""
        if days <= 0:
            return 0
        return self._write("DELETE FROM events WHERE created<? AND seq<(SELECT MAX(seq) FROM events)", (time.time() - days * 86400,))


    # Notes: what an agent or a person decided about a message ("kept unread:
    # exam moved"). They outlive the message in the index and are pruned by age.

    def add_note(self, mail_id: str, account: str, author: str, text: str) -> dict[str, Any]:
        with self._lock:
            cursor = self._db.execute("INSERT INTO notes (mail_id, account, author, created, text) VALUES (?, ?, ?, ?, ?)",
                                      (mail_id, account, author, time.time(), text))
            return self._note_row(self._db.execute("SELECT * FROM notes WHERE seq=?", (cursor.lastrowid,)).fetchone())

    @staticmethod
    def _note_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"seq": row["seq"], "mail_id": row["mail_id"], "account": row["account"], "author": row["author"],
                "created": row["created"], "text": row["text"]}

    def notes(self, mail_id: str = "", accounts: Iterable[str] = (), since: float = 0.0, limit: int = 100) -> list[dict[str, Any]]:
        """Notes, newest first. With `mail_id`, only that message's; with `accounts`, only theirs."""
        where, args = ["created>=?"], [since]
        if mail_id:
            where.append("mail_id=?")
            args.append(mail_id)
        names = list(dict.fromkeys(accounts))
        if names:
            where.append(f"account IN ({','.join('?' * len(names))})")
            args.extend(names)
        rows = self._rows(f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?",
                          (*args, max(1, min(limit, 1000))))
        return [self._note_row(row) for row in rows]

    def prune_notes(self, days: int) -> int:
        """Delete the notes that are older than `days`. 0 keeps every note."""
        if days <= 0:
            return 0
        return self._write("DELETE FROM notes WHERE created<?", (time.time() - days * 86400,))

    # Part cache

    def cache_path(self, identifier: str, name: str) -> Path:
        return self.cache_dir / identifier / name

    def read_cache(self, identifier: str, name: str) -> bytes | None:
        path = self.cache_path(identifier, name)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        path.touch()
        return data

    def write_cache(self, identifier: str, name: str, data: bytes, account: str, limit_bytes: int) -> None:
        path = self.cache_path(identifier, name)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        self.trim_cache(account, limit_bytes)

    def drop_cache(self, identifier: str) -> None:
        shutil.rmtree(self.cache_dir / identifier, ignore_errors=True)

    def trim_cache(self, account: str, limit_bytes: int) -> None:
        """Remove the least recently used cached parts above the account limit."""
        prefix = f"{account}."
        files = [
            (path.stat().st_mtime, path.stat().st_size, path)
            for directory in (self.cache_dir.iterdir() if self.cache_dir.exists() else [])
            if directory.name.startswith(prefix)
            for path in directory.iterdir() if path.is_file()
        ]
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= limit_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size
