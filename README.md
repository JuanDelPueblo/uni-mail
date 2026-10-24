# uni-mail

A small IMAP/OAuth mail data provider for AI agents, automation and scripts,
with no desktop mail client. It keeps an index of recent mail, serves it over
a socket, a CLI and MCP, and tells other programs when mail arrives.

uni-mail decides nothing about mail. It has no LLM, no triage, no agent and
no chat notifications. A consumer such as [Nanobot](https://github.com/HKUDS/nanobot)
gets an event, reads the mail through MCP, and decides what to do.

| Part | What it does |
| --- | --- |
| `uni-maild` | The daemon: IMAP and OAuth sign-in, the SQLite index, a Unix socket API and the event log. |
| `uni-mail` | The command line client, the event dispatcher (`uni-mail dispatch`) and the migration from hermes-mail. |
| `uni-mail-mcp` | An optional MCP server with the mail tools (extra `mcp`). |
| `deploy/` | systemd units (system services, and user services in `deploy/user/`), sysusers, tmpfiles and a config example. |
| `Containerfile`, `deploy/podman/` | A container image and rootless Podman Quadlet units. |
| `integrations/nanobot/` | Optional examples: a command that runs `nanobot trigger`, and a webhook receiver. |
| `integrations/openclaw/` | Optional example: the native `openclaw` destination and the MCP connection. |
| `integrations/hermes/` | Optional example: Hermes Agent with the MCP tools and a webhook route. |

The daemon and the dispatcher use only the Python standard library (Python
3.14 or later). Neither needs Nanobot or any agent framework.

- Install: [docs/INSTALL.md](docs/INSTALL.md), or with Podman: [docs/CONTAINER.md](docs/CONTAINER.md)
- Events, destinations, webhook signatures, delivery guarantees: [docs/EVENTS.md](docs/EVENTS.md)
- Design and its reasons: [docs/DESIGN.md](docs/DESIGN.md)
- Coming from hermes-mail: [docs/MIGRATION.md](docs/MIGRATION.md)
- Nanobot end to end: [integrations/nanobot/README.md](integrations/nanobot/README.md)
- Hermes Agent end to end: [integrations/hermes/README.md](integrations/hermes/README.md)

## Accounts

| Account                      | `provider`  | Sign-in                                        |
| ---------------------------- | ----------- | ---------------------------------------------- |
| Microsoft 365 work or school | `microsoft` | OAuth2 with the public Thunderbird client      |
| Personal Outlook.com         | `microsoft` | OAuth2 with the public Thunderbird client      |
| Gmail                        | `google`    | OAuth2 with the public Thunderbird client, or an app password |
| Other IMAP servers           | `imap`      | A password from a file                         |

The daemon does its own OAuth2 sign-in (PKCE, XOAUTH2) and keeps the refresh
token in its state directory with mode 0600; it saves a rotated refresh token
at once. It never reads the tokens of another mail client and never returns a
token on the socket. The Microsoft sign-in logs show the app as Mozilla
Thunderbird.

## Sending

`uni-mail send` sends from any signed-in OAuth account over SMTP with XOAUTH2
(`smtp.gmail.com`, `smtp.office365.com`, port 587 with STARTTLS), using the
stored refresh token. Gmail works with the existing sign-in. Microsoft
accounts need the `SMTP.Send` scope: run `uni-mail auth login <account> --send`
once. Both providers keep a copy in the Sent folder themselves. Password
accounts cannot send. `uni-mail send` has no attachments.

`uni-mail forward <mail-id> --account <sender> --to <addr>` forwards one indexed
message, with its attachments, as a new message from the sender account. The
original is read with `BODY.PEEK[]`, so it stays unread. Replies go to the
original sender (Reply-To). The same mail is not forwarded to the same address
twice unless you add `--force`.

## What the daemon downloads

The daemon never downloads a complete mailbox:

- It reads only the messages of the sync window (7 days by default), with
  `UID SEARCH SINCE`.
- For each message, it reads the flags, the size, the MIME structure, six
  header fields and a text preview of a few KiB.
- It downloads the full text or an attachment only when a client asks for it,
  and an attachment only as its own MIME part.
- Every read uses `BODY.PEEK`, so reading never marks mail as read.
- It opens folders read-only, except for an explicit read-state change (one
  `UID STORE` of `\Seen`) or an explicit archive. Archiving moves a message
  with `UID MOVE`, or, on a server without it, `UID COPY` then `UID STORE
  +\Deleted` then `UID EXPUNGE` of exactly those UIDs. Nothing else moves,
  deletes or expunges mail, and uni-mail never does any of it on its own.

## Command line

Every mail command prints JSON:

```sh
uni-mail status
uni-mail folders university
uni-mail list --since 2d --unread
uni-mail search "exam" --account university
uni-mail show <mail-id>
uni-mail attachments <mail-id>
uni-mail export-attachment <mail-id> <index>
uni-mail mark-read <mail-id> [<mail-id> ...]
uni-mail mark-unread <mail-id> [<mail-id> ...]
uni-mail archive <mail-id> [<mail-id> ...]
uni-mail send --account <name> --to <addr> --subject <text> (--body <text> | --body-file <path|->) [--cc <addr>] [--bcc <addr>] [--html] [--reply-to-message <mail-id>]
uni-mail forward <mail-id> --account <sender> --to <addr> [--to <addr>] [--note <text>] [--force]
uni-mail send-check <account>        # sign in to SMTP, send nothing
uni-mail events [--after N]
uni-mail auth login <account> [--send]
uni-mail account list | set <name> '<json>' [--password-stdin] | remove <name> | reset <name>
```

## Events

The daemon records `mail.new` for each new message (not for the mail that
the first sync finds), `mail.auth` when an account needs a new sign-in, and
`mail.error` when an account cannot sync for 30 minutes. Each event has a
stable `event_id` and metadata only (sender, subject, date, folder, ...),
never the text of the mail.

The dispatcher delivers each event to every destination that wants it, each
with its own queue, retries and acknowledgement:

- **command**: runs a configured program with the event JSON on stdin; exit
  code 0 acknowledges;
- **webhook**: a signed HTTPS POST (HMAC-SHA256, also as a Standard Webhooks
  signature); a 2xx answer acknowledges;
- **openclaw**: one agent turn per event in a shared OpenClaw conversation;
  acknowledged only when the run has finished
  ([integrations/openclaw](integrations/openclaw/README.md)).

```json
"dispatch": {
  "destinations": {
    "nanobot": {"type": "command", "argv": ["/usr/bin/python3", "/opt/uni-mail/integrations/nanobot/uni_mail_nanobot_trigger.py", "--trigger-id", "trg_XXXXXXXX"]},
    "audit": {"type": "webhook", "url": "https://hooks.example.net/mail", "secret_file": "/etc/uni-mail/secrets/audit", "events": ["mail.*"]}
  }
}
```

```sh
uni-mail dispatch run | once | status | check
uni-mail dispatch list [--status dead] | show <id> | retry <id> | test <destination> | replay <destination>
```

Delivery is at least once: deduplicate on `event_id`. See
[docs/EVENTS.md](docs/EVENTS.md).

## MCP

`uni-mail-mcp` (install with `pip install 'uni-mail[mcp]'`) serves these
tools over stdio, or over Streamable HTTP with a bearer token:
`mail_status`, `mail_list`, `mail_search`, `mail_show`, `mail_folders`,
`mail_attachments`, `mail_export_attachment`, `mail_mark_read`,
`mail_mark_unread`, `mail_archive`. It cannot sign in, change accounts or
destinations, or run commands. `--account` limits a server to some accounts and
`--read-only` leaves out the tools that change mail. The tools `mail_send` and `mail_forward` exist
only when you start the server with `--enable-send`, and then send only to the
addresses of `--send-allow` (none by default); `mail_forward` also needs
`--send-account`; see [docs/INSTALL.md](docs/INSTALL.md). A Nanobot config:

```json
{"tools": {"mcpServers": {"uni-mail": {"command": "/opt/uni-mail/bin/uni-mail-mcp", "toolTimeout": 60}}}}
```

## Development

```sh
python3.14 -m venv .venv && .venv/bin/pip install -e '.[mcp]' ruff
make test PYTHON=.venv/bin/python   # all tests; the MCP tests skip without the mcp package
make lint
```

The tests use an in-process fake IMAP server that records every command. They
fail when the daemon fetches a full message, uses `BODY` without `PEEK`, or
sends a bare `CLOSE` or `EXPUNGE`. A targeted `UID EXPUNGE` from the archive
fallback path is the only exception, and only for the UIDs named in that call.
No test needs a real account. `UNI_MAIL_TEST_NANOBOT=/path/to/nanobot` adds a
test against a real Nanobot; `UNI_MAIL_TEST_INSTALL=1` adds a test that
installs the package into a fresh venv.

`scripts/probe.py` checks whether an account allows direct IMAP access. It
changes nothing except one reversible read-state test:

```sh
python3 scripts/probe.py microsoft --user you@example.edu
```
