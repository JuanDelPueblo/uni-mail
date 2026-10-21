# uni-mail design

uni-mail gives any program access to recent mail over IMAP, with no desktop
mail client, and tells it when mail arrives. It has four parts, each with one
job:

| Part | Job | Runs as |
| --- | --- | --- |
| `uni-maild` | IMAP and OAuth, the index, the socket API, the event log | `uni-mail` |
| `uni-mail` | The command line client of the socket | any member of group `uni-mail` |
| `uni-mail dispatch run` | Delivers events to webhooks and commands | `uni-mail-dispatch` (or the consumer's user) |
| `uni-mail-mcp` | MCP server over the socket (optional extra) | the MCP client's user |

uni-mail decides nothing about mail. It never classifies a message, calls a
model, marks mail as read on its own, or sends a chat message. The programs
that receive its events (Nanobot, a script, a webhook receiver) do that.

```
IMAP <-> uni-maild --socket--> uni-mail CLI
            |      \--socket--> uni-mail-mcp <-stdio/HTTP-> MCP client (Nanobot, ...)
        event log
            |
     uni-mail dispatch --+--> command destination (stdin JSON) --> e.g. nanobot trigger
     (own SQLite queue)  +--> webhook destination (signed POST) --> e.g. integrations/nanobot receiver
```

## Accounts

uni-mail supports these account types:

| Account                          | Provider preset | Sign-in                                   |
| -------------------------------- | --------------- | ----------------------------------------- |
| Work or school Microsoft 365     | `microsoft`     | OAuth2 with the Thunderbird public client |
| Personal Outlook.com             | `microsoft`     | OAuth2 with the Thunderbird public client |
| Gmail                            | `google`        | OAuth2 with the Thunderbird public client, or an app password |
| Other IMAP servers               | `imap`          | Password from a file                      |

The service signs in with its own OAuth2 authorization. It never reads the
tokens of another mail client. The operator signs in one time in a browser,
with MFA when the account asks for it, and pastes the redirect URL into
`uni-mail auth login <account>`. Device-code sign-in is not used, because
restrictive tenants often block it.

The refresh token is mutable runtime state. The service keeps it in the state
directory with mode 0600. It is never in a config file, in the logs or in a
socket response, so no client, dispatcher destination or MCP client ever sees a token.

When a refresh fails, the service stops that account and sends one
`mail.auth` event. It does not retry in a loop, because each failure can count
against the account.

## Download limits

The service must never download a complete mailbox. These rules apply to
every account:

1. Each account has a sync window, `syncDays`, with a default of 7 days. The
   service finds messages with `UID SEARCH SINCE`. It never reads older mail.
2. The index stores only `UID`, `FLAGS`, `INTERNALDATE`, `RFC822.SIZE`,
   `BODYSTRUCTURE`, six header fields (Date, From, To, Cc, Subject and
   Message-ID) and a text preview of a maximum of 4000 characters. The preview
   is a partial fetch (`BODY.PEEK[<part>]<0.n>`) of the text part.
3. The service downloads a full text part only when a tool asks for the
   message. It downloads an attachment by its MIME part, never as the full
   message.
4. Every read uses `BODY.PEEK`, so a read never marks mail as read.
5. The message cache has a size limit for each account, and the service
   removes cached data when a message leaves the sync window.
6. A folder is opened read-only (`EXAMINE`) except for a read-state change or
   an explicit archive action. Only `uni-mail archive` (or the `mail_archive` MCP tool)
   moves, deletes or expunges mail, and only the UIDs it names: `UID MOVE`
   when the server has it, otherwise `UID COPY` + `UID STORE +\Deleted` +
   `UID EXPUNGE` of exactly those UIDs. Nothing else does. `uni-mail folders` also runs a read-only `LIST` to show the folder names.
7. The tests record the IMAP commands of each operation. They fail on any
   `BODY[]`, `RFC822` or full-folder fetch, and on a bare `CLOSE` or
   `EXPUNGE` outside the archive fallback path.

## Service

- Python 3.14 standard library only. Python 3.14 includes IMAP IDLE.
- One IMAP connection per account in IDLE on `INBOX`. The service restarts
  IDLE before the server timeout, and does a full window check every few
  minutes, because a server can drop IDLE without notice.
- One SQLite index for all accounts. A mail ID has the form
  `<account>.<16 hex digits>`. The digits are a hash of the account, the
  folder, `UIDVALIDITY` and the UID, so the ID is stable. The index also keeps
  the `Message-ID` header for idempotent follow-up actions.
- On the first sync, the service records existing mail and sends no event.
- A Unix socket with a JSON line protocol. The `uni-mail` CLI, the dispatcher and
  `uni-mail-mcp` use this socket.
- An append-only event log in the index: `mail.new` for new mail, `mail.auth`
  when an account needs a new sign-in, and `mail.error` when an account cannot
  sync for 30 minutes. Each event has a stable `event_id` and a snapshot of
  the mail metadata. Clients read the log with a cursor (`op: events, after:
  <seq>`); see docs/EVENTS.md.
- A read-state change is one `UID STORE` of `\Seen` on the server. An archive
  action is one `UID MOVE`, or, without it, `UID COPY` + `UID STORE
  +\Deleted` + `UID EXPUNGE` of exactly the named UIDs.

## Dispatcher

- A separate process with its own SQLite database (`dispatch.sqlite3` in
  `dispatch.state_dir`). The daemon has no outbound network access other than
  IMAP and OAuth, and it never runs a program.
- It reads the daemon's event log after its cursor, and stores each event with
  one delivery row for every destination that wants it, in one transaction
  with the new cursor. Then it tells the daemon (`events_consumed`) so that a
  fresh dispatcher database knows where to start.
- Each destination has its own worker thread and its own delivery rows, so
  its acknowledgement, retries and failures do not touch another destination.
- A worker claims a delivery with a lease (`lease_until` = timeout + 60 s). A
  second dispatcher process on the same database cannot claim it while the
  lease runs, and only the lease owner can record the result. After a crash
  the lease runs out and the delivery is attempted again.
- Failures back off exponentially with jitter, up to `max_attempts`; then the
  delivery is `dead` and stays visible until `uni-mail dispatch retry`.
- See docs/EVENTS.md for the event format, the webhook signature and the
  delivery guarantees.

## MCP server

`uni-mail-mcp` is a thin layer over the socket client, in the optional `mcp`
extra. It exposes only the mail operations: status, list, search, show,
folders, attachments, export, mark read or unread, archive (and, only when
started with `--enable-send`, send; see below). It cannot sign in,
change accounts, read the event log, configure destinations or run commands.

## Sending mail

The daemon has one new socket action, `send` (and `send_check`, which signs in
to SMTP and sends nothing). The CLI is `uni-mail send`. A second action,
`forward`, is described under "Forwarding mail".

**Transport.** SMTP with XOAUTH2, using the refresh token that IMAP already
uses. It is the least invasive choice: no new API, host or token store. The
Graph `sendMail` and Gmail API `messages.send` calls would need other scopes
(`Mail.Send`, `gmail.send`) that the Thunderbird client does not hold.

| Provider | Host | Scope needed | Present after the normal sign-in |
| --- | --- | --- | --- |
| `google` | `smtp.gmail.com:587` | `https://mail.google.com/` | yes |
| `microsoft` | `smtp.office365.com:587` | `https://outlook.office.com/SMTP.Send` | no: `uni-mail auth login <account> --send` |

The refresh token of a Microsoft account gives an SMTP access token by asking
the token endpoint for the `SMTP.Send` scope. If that account never consented,
the endpoint refuses, and `send` answers with the command to run. The
`--send` sign-in asks for the IMAP and the SMTP scope together, so the IMAP
access stays. A failed send never changes the stored token or the sync state
of the account. A tenant can still turn SMTP AUTH off; `send-check` shows it.

**Message.** Plain text, or HTML with a generated plain text part. The daemon
builds the headers itself (From is always the account address; Message-ID,
Date, and In-Reply-To and References for `--reply-to-message`). Recipients
are parsed to bare ASCII addresses and the same list goes into the headers
and the SMTP envelope; Bcc is only in the envelope. A subject with a line
break is refused. There are no attachments. Limits in the daemon: 998
characters of subject, 500000 of body, 50 recipients. A reply may only use an
account that holds the original message. Gmail and Exchange Online save SMTP
submissions in Sent, so the daemon appends nothing over IMAP.

**Logging.** Every send logs account, Message-ID, To, Cc, Bcc and subject,
never the body.

**MCP exposure.** The socket is trusted (same user), so the protections are in
`uni-mail-mcp`, which an agent that reads untrusted mail talks to:

- `mail_send` is not registered unless `--enable-send` is given.
- The allowlist (`--send-allow`, `$UNI_MAIL_MCP_SEND_ALLOW`) is empty by
  default and then allows nobody. An entry is an address or `@domain`. Every
  To and Cc address must match, or nothing is sent. The tool has no Bcc.
- `--send-account` fixes the sender account.
- A sliding window of 10 sends per hour (`--send-rate-limit`), a body of 20000
  characters and a subject of 200 (`--send-max-body`, `--send-max-subject`),
  at most 10 recipients. The tool sends plain text only.
- Every send and every refusal is logged by the MCP server (to stderr, at
  INFO whatever `--log-level` is), without the body.
- The tool description and the server instructions tell the model that mail
  text is not an instruction. That is advice only; the allowlist is the control.

`mail_forward` follows the same rules; see "Forwarding mail".

Limits of this design: the allowlist bounds who can get mail, not what it says,
so a prompt-injected agent can still put attacker-chosen text in a message
to an allowed recipient. A running server keeps its policy until restarted.

## Forwarding mail

The socket action `forward` (CLI: `uni-mail forward MAIL_ID --account SENDER
--to ADDRESS [--to ...] [--note TEXT] [--force]`) resends one indexed message.
It is a new message sent from the sender account's own SMTP login, not a
redirect, so the envelope sender is the sender account. The sender may differ
from the account that holds the mail.

**Reading the original.** The daemon fetches the whole message with
`BODY.PEEK[]` over its action connection, with the folder selected read-only.
It does not set `\Seen` or any other flag and does not move anything. A message
larger than the account's `max_part_mb` is not fetched whole: its text goes
out from the index and its attachments are named as left out.

**Message.** From is the sender address, To the recipients (no Cc or Bcc),
Subject `Fwd: <subject>` (an existing `Fwd:` or `Fw:` is not repeated),
Reply-To the original sender. There is no In-Reply-To or References: the
forward starts a new thread for other people. The plain text starts with
`Forwarded from <address of the original's account>`, then the original From,
Date and Subject, an optional `Note:` line, a blank line and the text. That is
what a phone notification shows. The text is the text/plain part; an HTML-only
message is converted with the same tag stripper as the index, which drops
scripts, styles and tags and loads nothing. Attachments stay attachments, up
to 15 MiB together (and the account's `max_part_mb`); the rest is left out and
named in the header lines of the text.

**Idempotency.** `forwarded.jsonl` in the state directory records every
mail ID and recipient that was forwarded (IDs and addresses, no text). A
recipient who already got that mail is skipped; when all are skipped the
answer is `forwarded: false` with `already_forwarded`, and nothing is sent.
A failed or refused delivery is not recorded. `--force` (the socket field
`force`, CLI only) sends again.

**Logging.** Each forward logs account, mail ID, Message-ID, recipients and
subject, never the text or the note.

**MCP exposure.** `mail_forward(mail_id, to, note)` exists only with
`--enable-send` and uses the same policy as `mail_send`: every recipient must
match `--send-allow`, the sender is always `--send-account` (without it the
tool refuses, and the caller cannot name an account), and a forward uses the
same hourly window as a send. The tool has no `force`; a repeat gets the
"already forwarded" answer. The CLI and the socket do not check an allowlist,
like `uni-mail send`: the socket user is trusted.

## Risks

- The Microsoft sign-in logs show the app as Mozilla Thunderbird. A tenant can
  see this as the use of another app's identity, and it can block the app at
  any time. The probe shows the current state only.
- A tenant policy can require a new sign-in after some days. The `mail.auth`
  event tells the operator when this occurs.
- Google can limit the Thunderbird client for the `https://mail.google.com/`
  scope. An app password is the fallback for Gmail.
- Delivery is at least once. If the dispatcher stops after a command ran or a
  webhook answered, and before it recorded the result, it delivers that event
  again after the restart, with the same event ID and delivery ID.
- A command destination runs with the rights of the dispatcher user, which is
  in the `uni-mail` group and so can read mail through the socket (not the
  tokens or passwords, which are mode 0600). Only the administrator can
  configure a destination; keep that user unprivileged.
