"""uni-mail: the command line client of uni-maild, and the event dispatcher.

Each mail command prints JSON. `uni-mail dispatch` reads the "dispatch"
section of the config file and delivers events; `uni-mail migrate` moves a
hermes-mail installation to uni-mail.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .client import Client, MailServiceError
from .config import ConfigError, default_config, resolve_socket


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=None))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="uni-mail", description="Read and manage recent mail through uni-maild, and dispatch its events")
    result.add_argument("--socket", default="", help="the daemon socket (default: $UNI_MAIL_SOCKET, the config file's socket or /run/uni-mail/mail.sock)")
    result.add_argument("--config", default=None,
                        help="the config file (default: $UNI_MAIL_CONFIG, ~/.config/uni-mail/config.json when it exists, "
                             "or /etc/uni-mail/config.json)")
    sub = result.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show the accounts, their sign-in state and the last sync")

    folders = sub.add_parser("folders", help="list the folders of an account on the mail server")
    folders.add_argument("account")

    for name in ("list", "search"):
        command = sub.add_parser(name, help="list recent mail" if name == "list" else "search sender, subject and preview")
        if name == "search":
            command.add_argument("query")
        command.add_argument("--account", default="")
        command.add_argument("--since", help="for example 2d, 12h, yesterday or 2026-09-01")
        command.add_argument("--until")
        command.add_argument("--unread", action="store_true")
        command.add_argument("--limit", type=int, default=50)

    show = sub.add_parser("show", help="show one message with its text")
    show.add_argument("mail_id")
    show.add_argument("--preview", action="store_true", help="show only the stored preview; do not download the text")

    attachments = sub.add_parser("attachments", help="list the attachments of one message")
    attachments.add_argument("mail_id")

    export = sub.add_parser("export-attachment", help="save an attachment in the export cache and print its path")
    export.add_argument("mail_id")
    export.add_argument("attachment_index", type=int)

    extract = sub.add_parser("extract-attachment", help="save an attachment at a path below the extract root")
    extract.add_argument("mail_id")
    extract.add_argument("attachment_index", type=int)
    extract.add_argument("output_path")

    for name in ("mark-read", "mark-unread"):
        command = sub.add_parser(name, help=f"{name.replace('-', ' ')} on the server")
        command.add_argument("mail_ids", nargs="+")

    archive = sub.add_parser("archive", help="move mail to the account's archive folder")
    archive.add_argument("mail_ids", nargs="+")

    events = sub.add_parser("events", help="show the daemon's event log")
    events.add_argument("--after", type=int, default=0, help="only the events after this sequence number")
    events.add_argument("--limit", type=int, default=100)

    auth = sub.add_parser("auth", help="sign in to an OAuth account")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_sub.add_parser("login", help="sign in with a browser")
    login.add_argument("account")
    login.add_argument("--redirect", help="the redirect URL; without it, the command asks for it")

    account = sub.add_parser("account", help="change the accounts of the running daemon (kept in its settings.json)")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    account_sub.add_parser("list", help="show every account with its settings and where they come from")
    save = account_sub.add_parser("set", help="add an account or replace its settings")
    save.add_argument("name")
    save.add_argument("settings", help='the settings as JSON, for example {"provider": "microsoft", "address": "me@example.edu"}')
    save.add_argument("--password-stdin", action="store_true", help="read the password of a password account from stdin")
    account_sub.add_parser("remove", help="remove an account").add_argument("name")
    account_sub.add_parser("reset", help="go back to the account of the config file").add_argument("name")

    dispatch = sub.add_parser("dispatch", help="deliver events to the destinations of the config file")
    dispatch_sub = dispatch.add_subparsers(dest="dispatch_command", required=True)
    dispatch_sub.add_parser("run", help="deliver events until stopped (the uni-mail-dispatch service)")
    dispatch_sub.add_parser("once", help="read the waiting events, make the due attempts, and exit")
    dispatch_sub.add_parser("status", help="show each destination's backlog and health")
    dispatch_sub.add_parser("check", help="check the dispatch section of the config file")
    listing = dispatch_sub.add_parser("list", help="list deliveries, newest first")
    listing.add_argument("--destination", default="")
    listing.add_argument("--status", default="", choices=["", "pending", "running", "delivered", "dead"])
    listing.add_argument("--event-id", default="")
    listing.add_argument("--limit", type=int, default=50)
    dispatch_sub.add_parser("show", help="show one delivery with its event and its last result").add_argument("delivery_id")
    retry = dispatch_sub.add_parser("retry", help="queue dead deliveries again with a new set of attempts")
    retry.add_argument("delivery_ids", nargs="*")
    retry.add_argument("--all-dead", action="store_true", help="every dead delivery (of --destination, if given)")
    retry.add_argument("--destination", default="")
    retry.add_argument("--force", action="store_true", help="also deliver again a delivery that succeeded")
    test = dispatch_sub.add_parser("test", help="send a synthetic event to one destination now")
    test.add_argument("destination")
    test.add_argument("--event-type", default="mail.new", choices=["mail.new", "mail.auth", "mail.error"])
    replay = dispatch_sub.add_parser("replay", help="queue the events still in the daemon's log for one destination")
    replay.add_argument("destination")
    replay.add_argument("--since", default="", help="only events from this time: 2d, 12h, yesterday or an ISO date")

    migrate = sub.add_parser("migrate", help="copy a hermes-mail installation to uni-mail (see docs/MIGRATION.md)")
    migrate.add_argument("--from-config", default="/etc/hermes-mail/config.json")
    migrate.add_argument("--user", default="", metavar="NAME",
                         help="migrate to a user service of this user: the config, state and socket go below the "
                              "user's home and runtime directory, owned by the user (see docs/MIGRATION.md)")
    migrate.add_argument("--to-config", default=None, help="the new config (default: /etc/uni-mail/config.json, "
                                                            "or ~NAME/.config/uni-mail/config.json with --user)")
    migrate.add_argument("--state-dir", default=None, help="the new state directory (default: /var/lib/uni-mail, "
                                                            "or ~NAME/.local/state/uni-mail with --user)")
    migrate.add_argument("--owner", default=None, help="the user and group of the new state when run as root "
                                                        "(default: uni-mail, or NAME with --user)")
    migrate.add_argument("--with-cache", action="store_true", help="also copy the part cache and the exports")
    migrate.add_argument("--apply", action="store_true", help="make the copy; without it, only report the plan")
    return result


def login(client: Client, account: str, redirect: str | None) -> dict[str, Any]:
    begin = client.auth_begin(account)
    if redirect is None:
        print("Open this URL in a browser and sign in:\n", file=sys.stderr)
        print(begin["url"], file=sys.stderr)
        print(
            f"\nAfter the sign-in, the browser goes to {begin['redirect_uri']}/?code=..."
            "\nThe page does not load. This is expected."
            "\nCopy the full URL from the address bar and paste it here.\n",
            file=sys.stderr,
        )
        redirect = input("Redirect URL: ")
    return client.auth_finish(account, redirect)


def _load_config(path: str) -> tuple[Any, Any]:
    from . import config as mail_config
    from .dispatch import config as dispatch_config

    cfg = mail_config.load(path)
    raw = json.loads(Path(path).read_text())
    return cfg, dispatch_config.parse(raw.get("dispatch"))


def _since(value: str) -> float:
    from .server import RequestError, parse_time

    try:
        return datetime.fromisoformat(parse_time(value)).timestamp()
    except RequestError as error:
        raise ConfigError(str(error)) from None


def run_dispatch(args: argparse.Namespace) -> Any:
    from .dispatch.runner import Dispatcher

    cfg, dispatch_cfg = _load_config(args.config)
    command = args.dispatch_command
    if command == "check":
        problems = {}
        for name, destination in dispatch_cfg.destinations.items():
            if destination.kind in ("webhook", "openclaw"):
                from .dispatch.webhook import standard_key

                try:
                    secret = destination.secret()
                    if not secret:
                        problems[name] = f"the secret variable {destination.secret_env} is empty"
                    elif destination.kind == "webhook":
                        standard_key(secret)
                except ConfigError as error:
                    problems[name] = str(error)
            elif not os.access(destination.argv[0], os.X_OK):
                problems[name] = f"{destination.argv[0]} is not an executable file for this user"
        result = {"destinations": sorted(dispatch_cfg.destinations), "state_dir": str(dispatch_cfg.state_dir), "problems": problems}
        if problems:
            result["ok"] = False
        return result
    client = Client(args.socket or str(cfg.socket))
    dispatcher = Dispatcher(dispatch_cfg, client)
    try:
        if command == "run":
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            dispatcher.run(stop)
            return None
        if command == "once":
            return dispatcher.run_once()
        if command == "status":
            return dispatcher.status()
        if command == "list":
            return {"deliveries": dispatcher.queue.list(destination=args.destination, status=args.status, event_id=args.event_id,
                                                        limit=args.limit)}
        if command == "show":
            delivery = dispatcher.queue.get(args.delivery_id)
            if delivery is None:
                raise ConfigError(f"no delivery {args.delivery_id} (or the prefix is not unique)")
            delivery.pop("body", None)
            return delivery
        if command == "retry":
            ids = list(args.delivery_ids)
            if args.all_dead:
                ids += [row["delivery_id"] for row in dispatcher.queue.list(destination=args.destination, status="dead", limit=1000)]
            if not ids:
                raise ConfigError("name a delivery ID, or use --all-dead")
            results = {}
            for identifier in ids:
                delivery = dispatcher.queue.get(identifier)
                if delivery is None:
                    results[identifier] = "not found"
                elif dispatcher.queue.retry(delivery["delivery_id"], force=args.force):
                    results[delivery["delivery_id"]] = "queued"
                else:
                    results[delivery["delivery_id"]] = f"not queued: it is {delivery['status']} (use --force for a delivered one)"
            return {"results": results}
        if command == "test":
            result = dispatcher.test(args.destination, args.event_type)
            if result["outcome"] != "delivered":
                result["ok"] = False
            return result
        if command == "replay":
            return {"destination": args.destination,
                    "queued": dispatcher.replay(args.destination, _since(args.since) if args.since else 0.0)}
    finally:
        dispatcher.close()
    raise SystemExit(f"unknown dispatch command {command}")


def run(args: argparse.Namespace) -> Any:
    command = args.command
    if args.config is None:
        args.config = default_config()
    if command == "dispatch":
        return run_dispatch(args)
    if command == "migrate":
        from . import migrate

        return migrate.run(args)
    client = Client(resolve_socket(args.socket, args.config))
    if command == "status":
        return client.status()
    if command == "folders":
        return client.folders(args.account)
    if command in ("list", "search"):
        return client.list(
            account=args.account, since=args.since, until=args.until, unread=args.unread,
            query=getattr(args, "query", ""), limit=args.limit,
        )
    if command == "show":
        return client.show(args.mail_id, full=not args.preview)
    if command == "attachments":
        return client.attachments(args.mail_id)
    if command == "export-attachment":
        return client.export_attachment(args.mail_id, args.attachment_index)
    if command == "extract-attachment":
        return client.extract_attachment(args.mail_id, args.attachment_index, args.output_path)
    if command in ("mark-read", "mark-unread"):
        return client.mark(args.mail_ids, command == "mark-read")
    if command == "archive":
        return client.archive(args.mail_ids)
    if command == "events":
        return client.events(after=args.after, limit=args.limit)
    if command == "auth":
        return login(client, args.account, args.redirect)
    if command == "account":
        sub = args.account_command
        if sub == "list":
            return client.settings()
        if sub == "set":
            try:
                settings = json.loads(args.settings)
            except ValueError as error:
                raise ConfigError(f"the settings are not JSON: {error}") from None
            password = sys.stdin.readline().rstrip("\n") if args.password_stdin else None
            return client.settings_save(args.name, settings, password)
        if sub == "remove":
            return client.settings_delete(args.name)
        if sub == "reset":
            return client.settings_reset(args.name)
    raise SystemExit(f"unknown command {command}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "dispatch":
        logging.basicConfig(level=os.environ.get("UNI_MAIL_LOG_LEVEL", "INFO").upper(), stream=sys.stderr,
                            format="%(levelname)s %(message)s")
    try:
        result = run(args)
    except MailServiceError as error:
        print(f"uni-mail: {error}", file=sys.stderr)
        response = getattr(error, "response", None)
        if response and "results" in response:
            _print(response)
        return 1
    except (ConfigError, KeyError, OSError) as error:
        print(f"uni-mail: {error.args[0] if isinstance(error, KeyError) else error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("uni-mail: cancelled", file=sys.stderr)
        return 1
    if result is None:
        return 0
    failed = result.pop("ok", True) is False
    _print(result)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
