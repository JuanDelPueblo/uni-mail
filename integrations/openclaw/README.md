# OpenClaw plugin

An [OpenClaw](https://docs.openclaw.ai/) plugin for uni-mail. It talks to the
daemon's socket directly: no MCP server, no hook endpoint, no tokens.

- **Mail tools, per agent.** One `access` entry per agent id says which
  accounts the agent sees and what it may do. An agent without an entry gets
  no mail tools; `*` is the entry of every agent not listed.
- **New mail starts a run.** Each `rules` entry sends matching events to an
  agent: one fresh run per event. The message names only the account, the mail
  ID and the event ID; the agent reads the mail with `mail_show`. The queue is
  OpenClaw's own ingress queue: it survives restarts, retries a failed run
  (3 attempts) and keeps the ones that still fail (the Mail page shows them).
- **Notes.** `mail_note` keeps a decision on a message in the daemon
  (`uni-mail notes` shows them); `mail_notes` reads them back, so one agent can
  see what another did with a mail.
- **Mail page.** A Mail entry in the Control UI sidebar shows the accounts,
  lets an operator sign in to an account again, lists the new-mail runs with a
  Retry for the failed ones, and shows recent notes.

Requires OpenClaw 2026.10.6 or newer and a running `uni-maild` that the
gateway's user can reach.

## Install

```sh
openclaw plugins install --link /path/to/uni-mail/integrations/openclaw --force --accept-capabilities
```

Sandboxed agents see plugin tools only when the sandbox tool policy lets them
through: add `group:plugins` (or the tool names) to
`tools.sandbox.tools.alsoAllow`. The plugin itself decides which mail tools
each agent gets.

## Settings

Edit them in the Control UI (Plugins → uni-mail → Settings) or under
`plugins.entries.uni-mail.config`:

```json5
{
  access: {
    // Every account, read only.
    main: { actions: ["read", "export"] },
    // One account; may mark read, archive, keep notes, and forward to one address.
    "mail-triage": {
      accounts: ["university"],
      actions: ["read", "mark", "archive", "note"],
      forward: { from: "gmail", to: ["me@example.com"], perHour: 10 },
    },
    // Reads every account and mails the brief to one address.
    university: { actions: ["read"], send: { from: "gmail", to: ["me@example.com"] } },
  },
  rules: [
    { agent: "mail-triage", accounts: ["university"] },
    // { agent: "main", events: ["mail.auth", "mail.error"] },  // sign-in and sync trouble
  ],
}
```

| Action | Tools |
| --- | --- |
| `read` (default) | `mail_status`, `mail_list`, `mail_search`, `mail_show`, `mail_folders`, `mail_attachments`, `mail_notes` |
| `mark` | `mail_mark_read`, `mail_mark_unread` |
| `archive` | `mail_archive` |
| `trash` | `mail_trash` (the provider empties the trash after about 30 days) |
| `export` | `mail_export_attachment`: copies the attachment into the agent's workspace |
| `note` | `mail_note` |
| `send` entry | `mail_send`: plain text from `from`, only to `to` (addresses, or `@domain`) |
| `forward` entry | `mail_forward`: from `from`, only to `to`; the same mail never goes to the same address twice |

Rule keys: `agent` (required), `accounts` (default all), `events` (default
`["mail.new"]`; shell patterns such as `mail.*`), `message` (placeholders
`{account}`, `{mail_id}`, `{event_id}`, `{event_type}`, `{detail}`),
`timeoutSeconds` (default 900), `enabled`.

A run counts as failed when it errors, times out, or its reply starts with
`AUTOMATION_FAILED`. The first start takes over from the first event that
nothing consumed yet (for example after `uni-mail-dispatch` stopped), else from
the newest event, so older mail starts no run.

`socket` (advanced) overrides the socket path. By default the plugin uses
`$UNI_MAIL_SOCKET`, then the `socket` of `~/.config/uni-mail/config.json`, then
`/run/uni-mail/mail.sock`.

## Mail page

The page needs **Settings → Labs → Custom plugin UI** in the Control UI, or in
the config:

```json5
{ gateway: { controlUi: { experimental: { customPlugins: true } } } }
```

Without it the plugin works as before; only the page is missing. The page is
open to every operator: reading needs the `operator.read` scope, signing in and
Retry need `operator.write`.

- **Accounts**: each account with its state, last sync, unread and all
  messages. **Sign in again** starts the OAuth sign-in (like
  `uni-mail auth login`): open the link, sign in, then paste the address the
  browser ended at (the page that does not load). Password accounts have no
  button (daemons older than this page do not say which accounts use a
  password, so every account gets the button and the daemon refuses).
- **New-mail runs**: the plugin's ingress queue. Running, queued (with the
  error of the last attempt) and failed runs; **Retry** queues a failed run
  again and the service starts it within a minute. With no `rules`, nothing
  follows new mail and a retried run does not start.
- **Recent notes**: the notes of the last day, 7, 30 or 90 days, newest first.

The page is plain browser JavaScript with no imports
(`dist/control-ui/mail-page.js` and `.css`, named in `openclaw.plugin.json`
under `controlUi`), so there is no build step. It refreshes every 30 seconds
while it is on screen. After a change to these files, restart the gateway or
use **Plugins → Advanced → Customize UI → Reload plugin UI**; the backend
(`src/page.ts`) changes with a plugin reload or a gateway restart.

## Tests

```sh
make test-openclaw   # node --test, Node 24 or newer
```
