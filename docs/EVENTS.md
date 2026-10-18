# Events and delivery

This is the contract for programs that consume uni-mail events. A consumer
does not need any uni-mail code.

## Event format (schema version 1)

```json
{
  "schema_version": 1,
  "event_id": "6f1c0a4e-3c1d-4f0b-9a57-0f2b8d4f6a11",
  "event_type": "mail.new",
  "seq": 42,
  "account": "university",
  "mail_id": "university.3f2a9c1d0b7e4a55",
  "timestamp": "2026-10-15T12:00:00.123456+00:00",
  "data": {
    "folder": "INBOX",
    "message_id": "<abc@example.edu>",
    "date": "2026-10-15T11:59:58+00:00",
    "sender": "Prof. Ada <ada@example.edu>",
    "recipients": "student@example.edu",
    "cc": "",
    "subject": "Exam moved to Monday",
    "read": false,
    "flagged": false,
    "size": 18234,
    "attachment_count": 1
  }
}
```

| Field | Meaning |
| --- | --- |
| `event_id` | Unique and stable: every retry and every destination sees the same ID. Deduplicate on it. |
| `event_type` | `mail.new`, `mail.auth` (an account needs a new sign-in) or `mail.error` (an account could not sync for 30 minutes). |
| `seq` | The position in the daemon's event log. Increases, but a destination can see events out of order after a retry. |
| `account` | The account name from the config. |
| `mail_id` | The stable mail ID for `mail.new`; `""` otherwise. Use it with `uni-mail show` or the `mail_show` MCP tool. |
| `timestamp` | When the daemon recorded the event (UTC, ISO 8601). |
| `data` | `mail.new`: the metadata above. `mail.auth`/`mail.error`: `{"detail": "<error text>"}`. A test event from `uni-mail dispatch test` has `"synthetic": true`, account `test` and mail ID `test.0000000000000000`. |

An event never contains the text of a message, an attachment, a password or
a token. Header values (`sender`, `subject`, ...) come from the sender of the
mail: treat them as untrusted text, and keep them out of command lines.

The first sync of an account (and the sync after a `UIDVALIDITY` change) only
records the existing mail; it creates no `mail.new` events.

New fields can appear in `data` in schema version 1. A change to an existing
field gets a new `schema_version`.

## Destinations

Destinations live in the `dispatch` section of `/etc/uni-mail/config.json`.
Only the administrator can add one: there is no socket, MCP or HTTP request
for it.

```json
"dispatch": {
  "state_dir": "/var/lib/uni-mail-dispatch",
  "retention_days": 30,
  "retry": {"max_attempts": 8, "initial_seconds": 30, "max_seconds": 3600},
  "destinations": {
    "nanobot": {"type": "command", "argv": ["/usr/bin/python3", "/opt/uni-mail/integrations/nanobot/uni_mail_nanobot_trigger.py", "--trigger-id", "trg_XXXXXXXX"]},
    "audit": {"type": "webhook", "url": "https://hooks.example.net/mail", "secret_file": "/etc/uni-mail/secrets/audit", "events": ["mail.*"]}
  }
}
```

Keys of every destination:

| Key | Default | Meaning |
| --- | --- | --- |
| `type` | (required) | `command`, `webhook` or `openclaw`. |
| `events` | `["mail.new"]` | Event types, with shell patterns such as `mail.*`. |
| `accounts` | all | Only events of these accounts. |
| `enabled` | `true` | `false` pauses the destination: its history stays, nothing new is queued for it, and its pending deliveries wait. Events that arrive while it is disabled are not queued later; use `uni-mail dispatch replay <destination> --since ...` after enabling it again. |
| `timeout_seconds` | 60 (command), 15 (webhook), 900 (openclaw) | The limit of one attempt. |
| `retry` | `dispatch.retry` | `max_attempts`, `initial_seconds`, `max_seconds` for this destination. |

Command keys: `argv` (a list; the first item an absolute path), `env` (extra
variables), `working_directory`, `permanent_exit_codes` (default `[64, 65]`).

Webhook keys: `url`, exactly one of `secret_file` (an absolute path, read at
each attempt, so a rotation needs no restart) or `secret_env` (a variable in
the dispatcher's environment), `allow_private_network` (default `false`),
`allow_http` (default `false`).

OpenClaw keys: the webhook keys, plus `agent_id` and `session_key` (both
required). The destination calls OpenClaw's `POST /hooks/agent` with a bearer
token in one persistent conversation, and only a finished run (`completion.status`
`ok`) counts as delivered; an ambiguous outcome is dead, not retried. See
[integrations/openclaw](../integrations/openclaw/README.md).

The dispatcher reads the config at start. Restart `uni-mail-dispatch` after a
change; `uni-mail dispatch check` validates it first.

## Command destinations

The dispatcher runs `argv` directly (no shell), with:

- stdin: the event JSON, in the canonical form (UTF-8, sorted keys, no spaces);
- a small environment: `PATH`, `HOME`, `LANG`, `LC_ALL`, `TZ`, the `env` of
  the destination, and `UNI_MAIL_EVENT_ID`, `UNI_MAIL_EVENT_TYPE`,
  `UNI_MAIL_DELIVERY_ID`, `UNI_MAIL_ATTEMPT`, `UNI_MAIL_DESTINATION`;
- its own process group, which is killed (SIGTERM, then SIGKILL) on timeout.

| Result | Outcome |
| --- | --- |
| exit 0 | delivered |
| exit code in `permanent_exit_codes` (default 64 `EX_USAGE`, 65 `EX_DATAERR`) | dead |
| any other exit code, a signal, a timeout, a program that cannot start | retried |

The last 4000 characters of stdout and stderr are kept with the delivery,
after redaction of known secrets and token-like text.

## Webhook destinations

The dispatcher sends `POST <url>` with the event JSON as the body and these
headers:

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `User-Agent` | `uni-mail/<version>` |
| `X-Uni-Mail-Event-Type` | the event type |
| `X-Uni-Mail-Event-Id` | the event ID |
| `X-Uni-Mail-Delivery-Id` | `dlv_` + 24 hex digits; the same for every attempt of this event to this destination |
| `X-Uni-Mail-Attempt` | 1, 2, ... |
| `X-Uni-Mail-Timestamp` | Unix time of this attempt, in seconds |
| `X-Uni-Mail-Signature` | `v1=<hex>` |
| `webhook-id` | the delivery ID ([Standard Webhooks](https://www.standardwebhooks.com/)) |
| `webhook-timestamp` | the same Unix time |
| `webhook-signature` | `v1,<base64>` |

### Signature

```
hex = HMAC-SHA256(key = secret, message = timestamp + "." + delivery_id + "." + body)
```

`secret` is the content of the secret file (or variable) without surrounding
white space, as UTF-8. `body` is the raw request body. To verify:

1. Refuse the request when `X-Uni-Mail-Timestamp` is more than 300 seconds
   from your clock.
2. Compute the HMAC over the raw body and compare it with each `v1=` value
   of `X-Uni-Mail-Signature` (comma-separated) in constant time.
3. Drop an `event_id` that you have already processed, and answer 2xx.

```python
import hashlib, hmac, time

def verify(secret: str, headers, body: bytes) -> bool:
    stamp = headers["X-Uni-Mail-Timestamp"]
    delivery = headers["X-Uni-Mail-Delivery-Id"]
    if not (stamp.isascii() and stamp.isdigit()) or abs(time.time() - int(stamp)) > 300:
        return False
    expected = "v1=" + hmac.new(secret.encode(), f"{stamp}.{delivery}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, value.strip()) for value in headers["X-Uni-Mail-Signature"].split(","))
```

The timestamp check limits a replay to five minutes; the `event_id` check
stops it inside that window.

### Standard Webhooks signature

The same request also carries a [Standard Webhooks](https://www.standardwebhooks.com/)
signature, so a receiver that supports the specification (Svix and Standard
Webhooks libraries, the Hermes webhook platform) needs no uni-mail code:

```
webhook-signature = "v1," + base64(HMAC-SHA256(key, webhook-id + "." + webhook-timestamp + "." + body))
```

`key` is the secret as UTF-8, except for a secret of the form
`whsec_<base64>`: then it is the decoded bytes, as the specification says.
`uni-mail dispatch check` refuses a `whsec_` secret that is not base64.
`webhook-id` is the delivery ID, the same for every attempt, so a receiver
that drops a `webhook-id` it has seen also drops retries.

### Answers

| Answer | Outcome |
| --- | --- |
| 2xx | delivered |
| 401, 403 | retried (the message says to check the secret; a rotation can cause this for a while) |
| 408, 409, 425, 429, 5xx, timeout, connection error | retried; `Retry-After` in seconds is honoured up to `max_seconds` |
| 3xx | dead: redirects are not followed |
| other 4xx | dead |

### Network rules

- `https` is required, except to a loopback address or with `allow_http`.
- The URL cannot hold a user name or password.
- The host is resolved at each attempt, and every address must be allowed;
  the connection goes to the checked addresses in order (no second lookup),
  with the certificate checked against the host name.
- Link-local addresses (including cloud metadata services), multicast and
  unspecified addresses are always refused. Private, loopback, carrier-grade
  NAT and unique-local addresses need `allow_private_network: true`.
- A refused address makes the delivery dead, without a request.
- The URL comes only from the config; nothing in an email can change it.

## Delivery guarantees

- **At least once.** A delivery is complete only when its result is stored.
  If the dispatcher stops after a command ran (or a webhook answered) and
  before it stored the result, the delivery is attempted again after the
  restart, with the same event ID and delivery ID. A command cannot be made
  exactly once without the consumer's help: deduplicate on `event_id`.
- **Independent destinations.** Each destination has its own delivery row,
  retries and acknowledgement. A failing or slow destination never delays
  another.
- **No head-of-line blocking.** A delivery waiting for a retry does not stop
  later events to the same destination, so order is not guaranteed.
- **One attempt at a time.** A delivery is leased to one worker; a second
  dispatcher process on the same database waits for the lease to end.
- **Crash recovery.** Stored deliveries survive a restart. Deliveries that
  were running when the dispatcher stopped are retried when their lease ends
  (the timeout plus 60 seconds).
- **No silent loss.** A delivery that runs out of attempts, or that a
  destination refuses for good, stays `dead` and visible in
  `uni-mail dispatch status` and `uni-mail dispatch list --status dead` until
  `retention_days` after its last change.

## Retention and new destinations

- The daemon keeps its event log for `event_retention_days` (default 14). The
  newest event always stays, so the sequence never restarts.
- The dispatcher deletes delivered and dead deliveries `retention_days`
  (default 30) after their last change. Pending deliveries stay until they
  finish.
- A new dispatcher database starts after the last event that a dispatcher
  stored, so it delivers the waiting events but not the history.
- A destination that is added later receives the events that arrive after it
  was added. To send it older events that are still in the daemon's log, run
  `uni-mail dispatch replay <destination> [--since 2d]`. Events that the
  destination already has are skipped.
- When the dispatcher is stopped for longer than `event_retention_days`, the
  events in between are deleted from the daemon's log before it reads them;
  it logs a warning with the missing sequence numbers.

## Command line

```sh
uni-mail dispatch check                 # validate the destinations and their secrets
uni-mail dispatch run                   # deliver until stopped (the systemd service)
uni-mail dispatch once                  # read waiting events, make the due attempts, exit
uni-mail dispatch status                # backlog and health of every destination
uni-mail dispatch list [--destination D] [--status dead] [--event-id E]
uni-mail dispatch show <delivery-id>    # a unique prefix is enough
uni-mail dispatch retry <delivery-id>... | --all-dead [--destination D] [--force]
uni-mail dispatch test <destination> [--event-type mail.new]
uni-mail dispatch replay <destination> [--since 2d]
uni-mail events [--after N]             # the daemon's event log
```
