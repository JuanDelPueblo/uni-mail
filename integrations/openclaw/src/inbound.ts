// New mail → agent runs. The service follows the daemon's event log, queues one
// delivery per matching rule in OpenClaw's ingress queue (it survives restarts,
// retries and keeps failed deliveries), and runs the rule's agent for each.

import { MailClient, type MailResponse } from "./client.ts";

export type Rule = {
  agent: string;
  accounts: string[];
  events: string[];
  message: string;
  timeoutSeconds: number;
};

export type Delivery = {
  agent: string;
  message: string;
  timeoutSeconds: number;
  eventId: string;
  eventType: string;
  account: string;
  mailId: string;
};

export const DEFAULT_MESSAGE =
  "New email in the {account} account. Mail ID: {mail_id} (event {event_id}). " +
  "Read it with mail_show and handle it as your instructions say. " +
  "If you cannot finish, reply AUTOMATION_FAILED on the first line and then the reason.";
const DEFAULT_OTHER_MESSAGE =
  "uni-mail reports {event_type} for the {account} account: {detail}. Handle it as your instructions say.";

const POLL_SECONDS = 50;
const ERROR_PAUSE_MS = 30_000;
const CURSOR_KEY = "events";

export function parseRules(config: any): Rule[] {
  const rules = Array.isArray(config?.rules) ? config.rules : [];
  return rules
    .filter((rule: any) => rule && typeof rule.agent === "string" && rule.agent.trim() && rule.enabled !== false)
    .map((rule: any) => ({
      agent: rule.agent.trim(),
      accounts: Array.isArray(rule.accounts) ? rule.accounts.filter((name: unknown) => typeof name === "string" && name) : [],
      events: Array.isArray(rule.events) && rule.events.length ? rule.events : ["mail.new"],
      message: typeof rule.message === "string" && rule.message.trim() ? rule.message : "",
      timeoutSeconds: Math.min(3600, Math.max(30, Number.isInteger(rule.timeoutSeconds) ? rule.timeoutSeconds : 900)),
    }));
}

/** Shell-style match of an event type: `mail.*` matches `mail.new`. */
function matches(pattern: string, value: string): boolean {
  const regex = new RegExp(`^${pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`);
  return regex.test(value);
}

export function deliveriesFor(event: MailResponse, rules: Rule[]): Delivery[] {
  const values: Record<string, string> = {
    account: String(event.account ?? ""),
    mail_id: String(event.mail_id ?? ""),
    event_id: String(event.event_id ?? ""),
    event_type: String(event.event_type ?? ""),
    // The error text of mail.auth and mail.error. Never the sender, the subject or the text of a mail.
    detail: String(event.data?.detail ?? "").slice(0, 500),
  };
  return rules
    .filter((rule) => rule.events.some((pattern) => matches(pattern, values.event_type)))
    .filter((rule) => !rule.accounts.length || rule.accounts.includes(values.account))
    .map((rule) => ({
      agent: rule.agent,
      timeoutSeconds: rule.timeoutSeconds,
      message: (rule.message || (values.event_type === "mail.new" ? DEFAULT_MESSAGE : DEFAULT_OTHER_MESSAGE)).replace(
        /\{(account|mail_id|event_id|event_type|detail)\}/g,
        (_, key: string) => values[key],
      ),
      eventId: values.event_id,
      eventType: values.event_type,
      account: values.account,
      mailId: values.mail_id,
    }));
}

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });

/** The service: start() runs the loop until the scheduler's signal aborts. */
export function inboundService(api: any, client: () => MailClient, rules: () => Rule[]) {
  let stop: AbortController | undefined;
  let loop: Promise<void> | undefined;

  async function runDelivery(claim: any): Promise<{ kind: "completed" } | { kind: "failed-retryable"; error: Error }> {
    const delivery: Delivery = claim.payload;
    const sessionKey = `agent:${delivery.agent}:subagent:uni-mail-${delivery.eventId}`;
    const run = await api.runtime.subagent.run({ sessionKey, message: delivery.message, deliver: false });
    const deadline = Date.now() + delivery.timeoutSeconds * 1000;
    let waited: any = { status: "pending" };
    while (waited.status === "pending" && Date.now() < deadline) {
      waited = await api.runtime.subagent.waitForRun({ runId: run.runId, timeoutMs: Math.min(60_000, deadline - Date.now()) });
    }
    if (waited.status !== "ok") {
      return { kind: "failed-retryable", error: new Error(`run ${run.runId} ${waited.status}${waited.error ? `: ${waited.error}` : ""}`) };
    }
    const { messages } = await api.runtime.subagent.getSessionMessages({ sessionKey: run.sessionKey ?? sessionKey, limit: 1 });
    const reply = messages?.at(-1);
    const text = typeof reply?.content === "string" ? reply.content : (reply?.content ?? []).map((part: any) => part?.text ?? "").join("");
    if (/^\s*AUTOMATION_FAILED/.test(text)) {
      return { kind: "failed-retryable", error: new Error(text.trim().slice(0, 1000)) };
    }
    api.logger.info(`uni-mail: ${delivery.eventType} ${delivery.mailId || delivery.account} handled by ${delivery.agent}`);
    return { kind: "completed" };
  }

  async function follow(signal: AbortSignal, logger: any) {
    const queue = api.runtime.state.openChannelIngressQueue({ accountId: "uni-mail" });
    const longest = Math.max(900, ...rules().map((rule) => rule.timeoutSeconds)) * 1000;
    const drain = api.runtime.state.openChannelIngressDrain({
      queue,
      dispatchClaimedEvent: (claim: any) => runDelivery(claim),
      deriveLaneKey: (record: any) => record.payload?.agent,
      retryPolicy: { maxAttempts: 3, baseMs: 60_000, maxMs: 15 * 60_000 },
      adoptionStallTimeoutMs: longest + 120_000,
      claimLeaseMs: longest + 120_000,
      onLog: (message: string) => logger.info(`uni-mail queue: ${message}`),
      abortSignal: signal,
    });
    const cursors = api.runtime.state.openKeyedStore({ namespace: "cursor", retention: "retained" });
    let cursor: number | undefined;
    let started = false;
    let followed = false;
    try {
      while (!signal.aborted) {
        try {
          if (!started) {
            await drain.recoverStaleClaims();
            cursor = (await cursors.lookup(CURSOR_KEY))?.seq;
            started = true;
          }
          if (cursor === undefined) {
            // The first start takes over from the first event that nothing has consumed yet
            // (a dispatcher that ran before), else from the newest event.
            const head = await client().request({ op: "event_head" });
            cursor = head.first_pending ? Number(head.first_pending) - 1 : Number(head.last ?? 0);
            await cursors.register(CURSOR_KEY, { seq: cursor });
          }
          if (!followed) {
            logger.info(`uni-mail: following events after ${cursor} for ${rules().map((rule) => rule.agent).join(", ")}`);
            followed = true;
          }
          const page = await client().request({ op: "events", after: cursor, timeout: POLL_SECONDS, limit: 100 }, (POLL_SECONDS + 30) * 1000, signal);
          const current = rules();
          for (const event of page.events ?? []) {
            for (const delivery of deliveriesFor(event, current)) {
              await queue.enqueue(`${delivery.eventId}:${delivery.agent}`, delivery, { laneKey: delivery.agent });
            }
            cursor = Number(event.seq);
          }
          if (page.events?.length) {
            await cursors.register(CURSOR_KEY, { seq: cursor });
            await client().request({ op: "events_consumed", upto: cursor });
          }
          await drain.drainOnce({ shouldStop: () => signal.aborted });
        } catch (error: any) {
          // A config reload replaces this plugin's runtime: the old loop ends quietly.
          if (signal.aborted || /no longer active/.test(String(error?.message))) break;
          logger.warn(`uni-mail: ${error?.message ?? error}; trying again in ${ERROR_PAUSE_MS / 1000} s`);
          await sleep(ERROR_PAUSE_MS, signal);
        }
      }
    } finally {
      try {
        drain.dispose();
      } catch {
        // The runtime may already be gone.
      }
    }
  }

  return {
    id: "uni-mail-inbound",
    apiVersion: 2,
    async start(context: any) {
      await this.stop();
      if (context.scheduler?.signal?.aborted) return;
      if (!rules().length) {
        context.logger.info("uni-mail: no new mail rules; nothing to follow");
        return;
      }
      stop = new AbortController();
      context.scheduler?.signal?.addEventListener("abort", () => stop?.abort(), { once: true });
      // Never let the loop reject: an unhandled rejection stops the whole gateway.
      loop = follow(stop.signal, context.logger).catch((error: any) => {
        if (!/no longer active/.test(String(error?.message))) context.logger.warn(`uni-mail: stopped following: ${error?.stack ?? error}`);
      });
    },
    async stop() {
      stop?.abort();
      await loop?.catch(() => undefined);
      stop = undefined;
      loop = undefined;
    },
  };
}
