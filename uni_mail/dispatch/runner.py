"""The dispatcher: read the daemon's event log and deliver each event to every
destination that wants it.

Delivery is at least once. An event can reach a destination twice, for
example when the dispatcher stops after a command ran but before it recorded
the exit code. Every attempt carries the same event ID and delivery ID, so a
consumer can drop the second one.

Each destination has its own worker thread, so a slow or failing destination
never holds up another one.
"""

from __future__ import annotations

import logging
import os
import random
import socket
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from .. import events
from ..client import Client, MailServiceError
from .command import CommandSink
from .config import HTTP_KINDS, Destination, DispatchConfig
from .openclaw import OpenClawSink
from .queue import Queue
from .sinks import Result, redact
from .webhook import WebhookSink

log = logging.getLogger("uni_mail.dispatch")

POLL_SECONDS = 25.0
IDLE_SECONDS = 30.0
PRUNE_SECONDS = 3600.0
PAGE = 200
LEASE_EXTRA = 60.0


def make_sink(destination: Destination, secrets: list[str]) -> Any:
    if destination.kind == "command":
        return CommandSink(destination, secrets)
    if destination.kind == "openclaw":
        return OpenClawSink(destination)
    return WebhookSink(destination)


def synthetic_event(event_type: str = "mail.new") -> dict[str, Any]:
    """An event for `uni-mail dispatch test`. Its account and mail ID do not exist."""
    data: dict[str, Any] = {"synthetic": True}
    if event_type == "mail.new":
        data.update(folder="INBOX", message_id="<test@uni-mail.invalid>", date=events.iso(time.time()),
                    sender="uni-mail test <test@uni-mail.invalid>", recipients="", cc="",
                    subject="uni-mail test event", read=False, flagged=False, size=0, attachment_count=0)
    else:
        data["detail"] = "uni-mail test event"
    return events.build(event_id=f"test-{uuid.uuid4()}", event_type=event_type, seq=0, account="test",
                        mail_id="test.0000000000000000" if event_type == "mail.new" else "", created=time.time(), data=data)


class Dispatcher:
    def __init__(self, cfg: DispatchConfig, client: Client, queue: Queue | None = None, owner: str = ""):
        self.cfg = cfg
        self.client = client
        self.queue = queue or Queue(cfg.state_dir)
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.destinations = {name: value for name, value in cfg.destinations.items() if value.enabled}
        self.secrets = self._secrets()
        self.sinks = {name: make_sink(value, self.secrets) for name, value in self.destinations.items()}
        self._wake = {name: threading.Event() for name in self.destinations}
        self._random = random.Random()
        self._ready = False

    def _secrets(self) -> list[str]:
        found = []
        for destination in self.cfg.destinations.values():
            if destination.kind in HTTP_KINDS:
                try:
                    found.append(destination.secret())
                except Exception:  # noqa: BLE001 - only for redaction
                    pass
        return [value for value in found if value]

    def close(self) -> None:
        self.queue.close()

    # Events in

    def prepare(self) -> int:
        """Set the cursor on the first run and record the destinations. Needs the daemon on the first run.

        A new dispatcher database starts after the events that a dispatcher
        already stored, so it delivers the events that were still waiting
        (for example the unacknowledged events of hermes-mail) but not the
        whole history. A destination that is added later starts at the cursor
        of that time; `uni-mail dispatch replay` sends it older events.
        """
        cursor = self.queue.cursor()
        if cursor is None:
            head = self.client.event_head()
            cursor = head["first_pending"] - 1 if head.get("first_pending") is not None else int(head.get("last") or 0)
            self.queue.set_cursor(cursor)
            log.info("new dispatch database: starting after event %d", cursor)
        if not self._ready:
            added = self.queue.sync_destinations({name: value.kind for name, value in self.destinations.items()})
            for name in added:
                log.info("destination %s: new; it receives the events after %d", name, cursor)
            self._ready = True
        return cursor

    def targets(self, event: dict[str, Any]) -> list[str]:
        return [name for name, destination in self.destinations.items()
                if destination.wants(event["event_type"], event["account"]) and event["seq"] > self.queue.start_seq(name)]

    def ingest(self, timeout: float = 0) -> int:
        """Store the next page of events and their deliveries. Return the number of events read."""
        cursor = self.prepare()
        response = self.client.events(after=cursor, timeout=timeout, limit=PAGE)
        batch = response["events"]
        if not batch:
            return 0
        if batch[0]["seq"] > cursor + 1 and cursor > 0:
            log.warning("events %d to %d left the daemon's log before the dispatcher read them", cursor + 1, batch[0]["seq"] - 1)
        targets: dict[str, list[str]] = {}
        for event in batch:
            try:
                events.validate(event)
            except ValueError as error:
                log.warning("event %s skipped: %s", event.get("seq"), error)
                continue
            targets[event["event_id"]] = self.targets(event)
        created = self.queue.ingest(batch, targets, batch[-1]["seq"], events.dumps)
        try:
            self.client.events_consumed(batch[-1]["seq"])
        except MailServiceError as error:
            log.warning("cannot mark the events as stored in the daemon: %s", error)
        if created:
            log.info("%d event(s), %d new deliveries", len(batch), created)
        for name in {name for names in targets.values() for name in names}:
            self._wake[name].set()
        return len(batch)

    # Deliveries out

    def deliver_one(self, name: str) -> bool:
        """Make one attempt for the next due delivery of a destination. Return False when none is due."""
        destination = self.destinations[name]
        delivery = self.queue.claim(name, self.owner, destination.timeout_seconds + LEASE_EXTRA)
        if delivery is None:
            return False
        attempt = delivery["attempts"]
        try:
            result = self.sinks[name].deliver(delivery["event"], delivery["body"].encode(), delivery["delivery_id"], attempt)
        except Exception as error:  # noqa: BLE001 - one bad delivery must not stop the worker
            log.exception("destination %s: delivery %s failed", name, delivery["delivery_id"])
            result = Result("retry", error=f"internal error: {type(error).__name__}: {error}")
        error = redact(result.error, self.secrets)
        output = redact(result.output, self.secrets)
        next_attempt = 0.0
        if result.outcome == "delivered":
            status = "delivered"
        elif result.outcome == "retry" and attempt < destination.retry.max_attempts:
            status = "pending"
            delay = destination.retry.delay(attempt) * self._random.uniform(0.8, 1.2)
            if result.retry_after:
                delay = max(delay, min(result.retry_after, destination.retry.max_seconds))
            next_attempt = time.time() + delay
        else:
            status = "dead"
            if result.outcome == "retry":
                error = f"{error}; gave up after {attempt} attempt(s)"
        if not self.queue.finish(delivery, self.owner, status, next_attempt=next_attempt, code=result.code, error=error, output=output):
            log.warning("destination %s: delivery %s lost its lease; the result is dropped", name, delivery["delivery_id"])
        elif status == "delivered":
            log.info("destination %s: delivered %s (%s %s)", name, delivery["delivery_id"], delivery["event_type"], delivery["event_id"])
        elif status == "pending":
            log.warning("destination %s: attempt %d of %s failed: %s; next in %.0f s", name, attempt, delivery["delivery_id"],
                        error, next_attempt - time.time())
        else:
            log.error("destination %s: delivery %s is dead: %s", name, delivery["delivery_id"], error)
        return True

    def test(self, name: str, event_type: str = "mail.new") -> dict[str, Any]:
        """Send a synthetic event to one destination now. Nothing is queued or recorded."""
        if name not in self.cfg.destinations:
            raise KeyError(f"unknown destination {name!r}")
        destination = self.cfg.destinations[name]
        sink = self.sinks.get(name) or make_sink(destination, self.secrets)
        event = synthetic_event(event_type)
        delivery = "dlv_test_" + uuid.uuid4().hex[:16]
        result = sink.deliver(event, events.dumps(event), delivery, 1)
        return {"destination": name, "delivery_id": delivery, "event": event, "outcome": result.outcome, "code": result.code,
                "error": redact(result.error, self.secrets), "output": redact(result.output, self.secrets)}

    def replay(self, name: str, since: float = 0.0) -> int:
        """Queue the events in the daemon's log (from `since`, a Unix time) for one destination. Return the new deliveries."""
        if name not in self.destinations:
            raise KeyError(f"unknown or disabled destination {name!r}")
        self.prepare()
        destination, after, created = self.destinations[name], 0, 0
        while True:
            batch = self.client.events(after=after, limit=PAGE)["events"]
            if not batch:
                return created
            wanted = [event for event in batch if destination.wants(event["event_type"], event["account"])
                      and datetime.fromisoformat(event["timestamp"]).timestamp() >= since]
            created += self.queue.add_deliveries(wanted, name, events.dumps)
            after = batch[-1]["seq"]

    # Loops

    def run_once(self) -> dict[str, Any]:
        """Read every waiting event, then make one pass over the due deliveries of all destinations at once."""
        read = 0
        while True:
            count = self.ingest()
            read += count
            if count < PAGE:
                break
        attempts = {name: 0 for name in self.destinations}

        def drain(name: str) -> None:
            while self.deliver_one(name):
                attempts[name] += 1

        threads = [threading.Thread(target=drain, args=(name,), name=f"dispatch-{name}") for name in self.destinations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.queue.prune(self.cfg.retention_days)
        return {"events": read, "attempts": attempts}

    def _ingest_loop(self, stop: threading.Event) -> None:
        backoff, pruned, failing = 5.0, 0.0, False
        while not stop.is_set():
            try:
                self.ingest(timeout=POLL_SECONDS)
                if failing:
                    log.info("the mail daemon answers again")
                backoff, failing = 5.0, False
            except MailServiceError as error:
                if not failing:
                    log.error("cannot read events: %s", error)
                failing = True
                stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception:  # noqa: BLE001 - the loop must not stop
                log.exception("reading events failed")
                stop.wait(backoff)
            if time.monotonic() - pruned > PRUNE_SECONDS:
                pruned = time.monotonic()
                self.queue.prune(self.cfg.retention_days)

    def _worker(self, name: str, stop: threading.Event) -> None:
        wake = self._wake[name]
        while not stop.is_set():
            try:
                if self.deliver_one(name):
                    continue
            except Exception:  # noqa: BLE001 - the worker must not stop
                log.exception("destination %s: worker error", name)
            due = self.queue.next_due(name)
            wait = IDLE_SECONDS if due is None else min(max(due - time.time(), 0.05), IDLE_SECONDS)
            wake.wait(wait)
            wake.clear()

    def run(self, stop: threading.Event) -> None:
        threads = [threading.Thread(target=self._ingest_loop, args=(stop,), name="dispatch-ingest", daemon=True)]
        threads += [threading.Thread(target=self._worker, args=(name, stop), name=f"dispatch-{name}", daemon=True)
                    for name in self.destinations]
        for thread in threads:
            thread.start()
        log.info("dispatching to %d destination(s): %s", len(self.destinations), ", ".join(sorted(self.destinations)) or "none")
        stop.wait()
        for name in self.destinations:
            self._wake[name].set()
        # A running command or request finishes first; the ingest thread may still wait on the daemon.
        for thread in threads[1:]:
            thread.join()

    def status(self) -> dict[str, Any]:
        stats = self.queue.stats()

        def iso(value: float) -> str:
            return events.iso(value) if value else ""

        destinations = {}
        for name, entry in sorted(stats.items()):
            configured = self.cfg.destinations.get(name)
            destinations[name] = {
                **entry,
                "configured": configured is not None,
                "enabled": bool(configured and configured.enabled),
                "oldest_pending": iso(entry.get("oldest_pending", 0.0)),
                "last_delivered": iso(entry.get("last_delivered", 0.0)),
                "last_failure": iso(entry.get("last_failure", 0.0)),
            }
        for name, destination in self.cfg.destinations.items():
            destinations.setdefault(name, {"kind": destination.kind, "configured": True, "enabled": destination.enabled,
                                           "note": "not started yet"})
        result: dict[str, Any] = {"cursor": self.queue.cursor(), "database": str(self.queue.path), "destinations": destinations}
        try:
            result["daemon"] = self.client.event_head()
            result["daemon"].pop("ok", None)
        except MailServiceError as error:
            result["daemon"] = {"error": str(error)}
        return result
