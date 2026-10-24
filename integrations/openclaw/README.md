# OpenClaw

uni-mail delivers each new email to [OpenClaw](https://docs.openclaw.ai/) with
a native `openclaw` dispatch destination: no trigger script in between. Every
matching email becomes one agent turn, and the agent reads the mail through the
uni-mail MCP server. By default each turn is a fresh conversation
(`sessionMode: "isolated"`), so no email stays in the context of the next one.
With a `session_key`, every email is a turn in that **same** persistent
conversation instead.

uni-mail decides nothing. What is essential and whether to mark mail as read
belongs to the agent's policy (its instructions in OpenClaw). The request
message holds only the account, the mail ID and the event ID, plus an
instruction to fetch the message over MCP and follow that policy; no subject,
sender or body is ever put in it.

Checked against the OpenClaw docs (`automation/cron-jobs/webhooks`,
`gateway/config-hooks`, `cli/mcp/transports`), October 2026.

## 1. OpenClaw: enable hooks

```json
{
  "hooks": {
    "enabled": true,
    "path": "/hooks",
    "token": "<long random value, only for hooks>",
    "allowedAgentIds": ["mail-triage"]
  }
}
```

Use a token dedicated to hooks, not the gateway token. The agent `mail-triage`
must exist and carry your triage policy. A destination with a `session_key`
(a persistent conversation) also needs `allowRequestSessionKey: true` and a
non-empty `allowedSessionKeyPrefixes` that matches the key.

## 2. OpenClaw: the uni-mail MCP connection

Run uni-mail's own Streamable HTTP server (nothing else to install):

```sh
openssl rand -hex 32 > ~/.config/uni-mail/secrets/mcp-token && chmod 0600 ~/.config/uni-mail/secrets/mcp-token
uni-mail-mcp --transport streamable-http --host 127.0.0.1 --port 8765 --token-file ~/.config/uni-mail/secrets/mcp-token \
  --account university
```

`--account` (repeat it for more) keeps the triage agent to the accounts it
triages: it cannot read or archive mail of the others. Give other agents a
server of their own, `--read-only` if they only need to read.

and point OpenClaw at it (`MCP_TOKEN` set in the gateway's environment):

```json
{
  "mcp": {
    "servers": {
      "uni-mail": {
        "url": "http://127.0.0.1:8765/mcp",
        "transport": "streamable-http",
        "headers": {"Authorization": "Bearer ${MCP_TOKEN}"}
      }
    }
  }
}
```

The default path is `/mcp`; `openclaw mcp probe` lists the tools.

## 3. uni-mail: the destination

```sh
openssl rand -hex 32 > ~/.config/uni-mail/secrets/openclaw   # the same value as hooks.token
chmod 0600 ~/.config/uni-mail/secrets/openclaw
```

```json
"dispatch": {
  "destinations": {
    "openclaw": {
      "type": "openclaw",
      "url": "http://127.0.0.1:18789/hooks/agent",
      "secret_file": "/home/you/.config/uni-mail/secrets/openclaw",
      "agent_id": "mail-triage",
      "accounts": ["university"],
      "events": ["mail.new"],
      "allow_private_network": true,
      "timeout_seconds": 900
    }
  }
}
```

`secret_env` (a variable of the dispatcher) replaces `secret_file`. The token
is read at each attempt. Then `uni-mail dispatch check`, `uni-mail dispatch
test openclaw` (a synthetic event that does not exist in any mailbox) and a
restart of `uni-mail-dispatch`.

## What the dispatcher sends

`POST /hooks/agent` with `Authorization: Bearer <token>`, `Idempotency-Key:
<delivery ID>` (the same for every retry) and

```json
{"message": "...", "name": "uni-mail", "agentId": "mail-triage", "sessionMode": "isolated",
 "deliver": false, "waitForCompletion": true, "timeoutSeconds": 885}
```

With a `session_key`, `"sessionMode": "persistent"` and `"sessionKey": "<key>"`
replace `"sessionMode": "isolated"`.

`timeoutSeconds` is `timeout_seconds` minus 15, so the agent turn ends before
the request gives up. `deliver: false` keeps OpenClaw from announcing anywhere;
the agent acts through its own tools.

## Acknowledgement and failures

OpenClaw's `200 {"ok": true, "runId": ...}` means only that the run was
admitted. With `waitForCompletion` the `200` also has `completion.status`;
only `"ok"` marks the delivery delivered.

| Result | Delivery |
| --- | --- |
| `200` and `completion.status: "ok"` | delivered |
| cannot connect; `401`, `403`, `408`, `429`, `502`, `503` (refused before the run was admitted) | retried with the queue's backoff, `Retry-After` honoured |
| `400`, `404`, `405`, `409`, `413`, other `4xx`, redirects | dead (the request is wrong) |
| timeout or reset after the request was sent; `200` without a finished `completion`, or with `error`/`skipped`; other `5xx` | **dead**, not retried: the agent may already have read or marked the mail |

A dead delivery shows in `uni-mail dispatch list --status dead`. After you
have checked OpenClaw, `uni-mail dispatch retry <id>` runs it again.

This is not exactly-once. OpenClaw remembers an `Idempotency-Key` only while a
run is unsettled and for 5 minutes after, and forgets it on a restart; a
dispatcher crash between the answer and the stored result sends the delivery
again later with the same key. Make the agent's policy safe to repeat (reading,
classifying and marking read are), and let it skip an event ID it has seen.
With a `session_key`, all emails share one conversation, which OpenClaw
serializes: a long turn delays the next ones, and `timeout_seconds` should cover
the queue wait.
