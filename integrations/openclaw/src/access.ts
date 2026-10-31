// Who may do what: the plugin's `access` setting, resolved for one agent.

export const ACTIONS = ["read", "mark", "archive", "trash", "export", "note"] as const;
export type Action = (typeof ACTIONS)[number];

export type Outbound = { from: string; to: string[]; perHour: number };

export type Access = {
  agentId: string;
  /** Empty: every account. */
  accounts: string[];
  actions: Set<Action>;
  send?: Outbound;
  forward?: Outbound;
};

export const MAX_RECIPIENTS = 10;
export const MAX_SUBJECT = 200;
export const MAX_BODY = 20_000;
export const MAX_NOTE = 2_000;

/** The access of an agent: its own entry, else the `*` entry, else none. */
export function accessFor(config: any, agentId: string | undefined): Access | undefined {
  if (!agentId) return undefined;
  const table = config?.access ?? {};
  const entry = table[agentId] ?? table["*"];
  if (!entry || typeof entry !== "object") return undefined;
  const actions = new Set<Action>(
    (Array.isArray(entry.actions) ? entry.actions : ["read"]).filter((name: string) =>
      (ACTIONS as readonly string[]).includes(name),
    ),
  );
  return {
    agentId,
    accounts: uniqueStrings(entry.accounts),
    actions,
    send: outbound(entry.send),
    forward: outbound(entry.forward),
  };
}

function outbound(raw: any): Outbound | undefined {
  if (!raw || typeof raw !== "object" || typeof raw.from !== "string" || !raw.from) return undefined;
  return { from: raw.from, to: normalizeAllow(uniqueStrings(raw.to)), perHour: clamp(raw.perHour, 1, 100, 10) };
}

function uniqueStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((item): item is string => typeof item === "string" && item.trim() !== "").map((item) => item.trim()))];
}

function clamp(value: unknown, low: number, high: number, fallback: number): number {
  const number = typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : fallback;
  return Math.min(high, Math.max(low, number));
}

const ADDRESS = /^[^@\s<>,;"]+@[^@\s<>,;"]+\.[^@\s<>,;"]+$/;

/** Allow-list entries in lower case: full addresses, or @domain (also written *@domain). */
export function normalizeAllow(entries: string[]): string[] {
  return [
    ...new Set(
      entries
        .map((entry) => entry.trim().toLowerCase().replace(/^\*@/, "@"))
        .filter((entry) => (entry.startsWith("@") ? ADDRESS.test(`x${entry}`) : ADDRESS.test(entry))),
    ),
  ];
}

/** The bare address of `Name <addr>` or `addr`, in lower case; undefined if it is not one address. */
export function bareAddress(value: string): string | undefined {
  const match = /<([^<>]+)>\s*$/.exec(value);
  const address = (match ? match[1] : value).trim().toLowerCase();
  return ADDRESS.test(address) ? address : undefined;
}

export function allowed(allow: string[], address: string): boolean {
  return allow.includes(address) || allow.includes(`@${address.split("@")[1]}`);
}

/** The account of a mail ID: the part before the first dot (`university.3f2a9c1d0b7e4a55`). */
export function accountOf(mailId: string): string {
  return mailId.trim().split(".")[0];
}

export function checkAccount(access: Access, account: string): void {
  if (access.accounts.length && !access.accounts.includes(account)) {
    throw new Error(`agent ${access.agentId} may use only the account(s) ${access.accounts.join(", ")}, not ${JSON.stringify(account)}`);
  }
}

export function checkMail(access: Access, ...mailIds: string[]): void {
  for (const mailId of mailIds) {
    if (access.accounts.length && !access.accounts.includes(accountOf(mailId))) {
      throw new Error(`${JSON.stringify(mailId)} is not mail of the account(s) ${access.accounts.join(", ")} that agent ${access.agentId} may use`);
    }
  }
}

/** Sends per agent and kind in the last hour. A refused or failed send still counts. */
export class RateLimiter {
  private readonly sent = new Map<string, number[]>();
  private readonly now: () => number;

  constructor(now: () => number = Date.now) {
    this.now = now;
  }

  reserve(key: string, perHour: number): void {
    const now = this.now();
    const recent = (this.sent.get(key) ?? []).filter((time) => now - time < 3_600_000);
    if (recent.length >= perHour) {
      this.sent.set(key, recent);
      throw new Error(`send limit reached: at most ${perHour} messages per hour. Nothing was sent.`);
    }
    recent.push(now);
    this.sent.set(key, recent);
  }
}
