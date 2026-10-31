// The backend of the Mail page in the Control UI. The page (dist/control-ui/)
// calls these operations through `plugins.sessionAction`, the transport of
// OpenClaw's feature contracts: a query needs operator.read, an action
// operator.write, and the Gateway checks the input against its schema.
// Errors come back as `{ ok: false, error }` so the page can show the reason.

import type { MailClient, MailResponse } from "./client.ts";
import { openDeliveryQueue } from "./inbound.ts";

const LIST_LIMIT = 50; // per queue list; a session action result holds at most 4096 JSON values
const NOTE_LIMIT = 100;
const NOTE_CHARS = 1000;
const ERROR_CHARS = 1000;

type Logger = { info(message: string): void; warn(message: string): void };

export type PageOperation = {
  kind: "query" | "action";
  description: string;
  input: Record<string, unknown>;
  run(input: any): Promise<unknown>;
};

function object(properties: Record<string, unknown>, required: string[] = []) {
  return { type: "object", properties, required, additionalProperties: false };
}

const accountName = { type: "string", minLength: 1, maxLength: 200 };
const clip = (value: unknown, limit: number) => (typeof value === "string" ? value.slice(0, limit) : "");
const time = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : null);

function account(entry: MailResponse) {
  return {
    name: clip(entry.name, 200),
    provider: clip(entry.provider, 100),
    address: clip(entry.address, 320),
    // Older daemons do not report it; the page then offers the sign-in for every account.
    auth: clip(entry.auth, 20),
    status: clip(entry.status, 50),
    error: clip(entry.error, ERROR_CHARS),
    lastSync: clip(entry.last_sync, 50),
    syncDays: time(entry.sync_days),
    messages: time(entry.messages) ?? 0,
    unread: time(entry.unread) ?? 0,
  };
}

/** One delivery of the queue, without its message text. */
function delivery(record: any) {
  const payload = record?.payload ?? {};
  return {
    id: clip(record?.id, 300),
    agent: clip(payload.agent ?? record?.laneKey, 200),
    account: clip(payload.account, 200),
    mailId: clip(payload.mailId, 300),
    eventType: clip(payload.eventType, 100),
    attempts: time(record?.attempts) ?? 0,
    lastError: clip(record?.message || record?.lastError, ERROR_CHARS),
    receivedAt: time(record?.receivedAt),
    lastAttemptAt: time(record?.lastAttemptAt),
    claimedAt: time(record?.claim?.claimedAt),
    failedAt: time(record?.failedAt),
    reason: clip(record?.reason, 200),
    retryable: record?.payload !== undefined,
  };
}

const RETRY_REFUSED: Record<string, string> = {
  "not-found": "there is no failed run with this ID (it may have been retried already)",
  completed: "this run has completed already",
  active: "this run is queued or running already",
  unrecoverable: "this failed run kept no delivery to run again",
};

export function pageOperations(
  api: any,
  client: () => MailClient,
  following: () => boolean,
  logger: Logger = api.logger,
): Record<string, PageOperation> {
  return {
    accounts: {
      kind: "query",
      description: "Every mail account with its sign-in state, last sync and counts.",
      input: object({}),
      async run() {
        const status = await client().request({ op: "status" }, 30_000);
        return { accounts: (status.accounts ?? []).map(account) };
      },
    },
    "auth.begin": {
      kind: "action",
      description: "Start the sign-in of an OAuth account: the URL to open and the redirect URI it ends at.",
      input: object({ account: accountName, send: { type: "boolean" } }, ["account"]),
      async run({ account: name, send }) {
        const begun = await client().request({ op: "auth_begin", account: name, send: Boolean(send) }, 30_000);
        logger.info(`uni-mail: sign-in of ${name} started from the Mail page`);
        return { account: name, url: clip(begun.url, 8000), redirectUri: clip(begun.redirect_uri, 2000) };
      },
    },
    "auth.finish": {
      kind: "action",
      description: "Finish the sign-in with the URL the browser ended at.",
      input: object({ account: accountName, redirect: { type: "string", minLength: 1, maxLength: 8000 } }, ["account", "redirect"]),
      async run({ account: name, redirect }) {
        const done = await client().request({ op: "auth_finish", account: name, redirect: redirect.trim() }, 60_000);
        logger.info(`uni-mail: ${name} signed in again from the Mail page`);
        return { account: name, message: clip(done.message, 500) || "signed in" };
      },
    },
    queue: {
      kind: "query",
      description: "The new-mail runs: queued, running and failed deliveries.",
      input: object({}),
      async run() {
        const queue = openDeliveryQueue(api);
        const [pending, claims, failed] = await Promise.all([
          queue.listPending({ limit: LIST_LIMIT, orderBy: "received" }),
          queue.listClaims(),
          queue.listFailed ? queue.listFailed({ limit: LIST_LIMIT }) : [],
        ]);
        const newest = (left: any, right: any) => (right.failedAt ?? 0) - (left.failedAt ?? 0);
        return {
          following: following(),
          pending: pending.slice(0, LIST_LIMIT).map(delivery),
          running: claims.slice(0, LIST_LIMIT).map(delivery),
          failed: failed.map(delivery).sort(newest).slice(0, LIST_LIMIT),
        };
      },
    },
    "queue.retry": {
      kind: "action",
      description: "Queue a failed run again.",
      input: object({ id: { type: "string", minLength: 1, maxLength: 300 } }, ["id"]),
      async run({ id }) {
        const queue = openDeliveryQueue(api);
        if (!queue.resubmit) throw new Error("this OpenClaw cannot queue a failed run again");
        const outcome = await queue.resubmit(id);
        if (outcome.kind !== "resubmitted") throw new Error(RETRY_REFUSED[outcome.kind] ?? `the queue refused: ${outcome.kind}`);
        logger.info(`uni-mail: failed run ${id} queued again from the Mail page`);
        return { id, retried: true };
      },
    },
    notes: {
      kind: "query",
      description: "Recent notes on mail, newest first.",
      input: object({
        days: { type: "integer", minimum: 1, maximum: 90 },
        limit: { type: "integer", minimum: 1, maximum: NOTE_LIMIT },
      }),
      async run({ days = 7, limit = NOTE_LIMIT }) {
        const found = await client().request({ op: "notes", since: `${days}d`, limit }, 30_000);
        return {
          days,
          notes: (found.notes ?? []).slice(0, limit).map((note: MailResponse) => ({
            seq: time(note.seq) ?? 0,
            mailId: clip(note.mail_id, 300),
            account: clip(note.account, 200),
            author: clip(note.author, 100),
            created: typeof note.created === "number" ? Math.round(note.created * 1000) : null,
            text: clip(note.text, NOTE_CHARS),
          })),
        };
      },
    },
  };
}

/** Each operation becomes a session action that only an operator with the right scope may call. */
export function registerPageOperations(api: any, operations: Record<string, PageOperation>) {
  const owner = typeof api.session?.controls?.registerSessionAction === "function" ? api.session.controls : api;
  if (typeof owner.registerSessionAction !== "function") return;
  for (const [id, operation] of Object.entries(operations)) {
    owner.registerSessionAction({
      id,
      description: operation.description,
      schema: operation.input,
      requiredScopes: [operation.kind === "query" ? "operator.read" : "operator.write"],
      async handler(context: any) {
        try {
          return { ok: true, result: await operation.run(context.payload ?? {}) };
        } catch (error: any) {
          return { ok: false, error: String(error?.message ?? error).slice(0, ERROR_CHARS), code: "UNI_MAIL" };
        }
      },
    });
  }
}
