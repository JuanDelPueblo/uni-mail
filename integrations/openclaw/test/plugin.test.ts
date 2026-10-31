// node --test integrations/openclaw/test/ (Node 24 strips the types).

import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { after, before, describe, test } from "node:test";

import { accessFor, normalizeAllow, RateLimiter } from "../src/access.ts";
import { MailClient } from "../src/client.ts";
import { DEFAULT_MESSAGE, deliveriesFor, parseRules } from "../src/inbound.ts";
import { pageOperations, registerPageOperations } from "../src/page.ts";
import { mailTools } from "../src/tools.ts";

const quiet = { info() {}, warn() {} };

/** A fake uni-maild: answers each request with handler(request) (no answer when it returns undefined), and records the requests. */
async function fakeDaemon(handler: (request: any) => any) {
  const dir = await mkdtemp(path.join(os.tmpdir(), "uni-mail-plugin-"));
  const socket = path.join(dir, "mail.sock");
  const requests: any[] = [];
  const connections = new Set<net.Socket>();
  const server = net.createServer((connection) => {
    connections.add(connection);
    connection.on("close", () => connections.delete(connection));
    let buffer = "";
    connection.on("data", (chunk) => {
      buffer += chunk;
      if (!buffer.endsWith("\n")) return;
      const request = JSON.parse(buffer);
      requests.push(request);
      const response = handler(request);
      if (response !== undefined) connection.end(`${JSON.stringify(response)}\n`);
    });
  });
  await new Promise<void>((resolve) => server.listen(socket, resolve));
  return {
    socket,
    dir,
    requests,
    close: async () => {
      for (const connection of connections) connection.destroy();
      await new Promise((resolve) => server.close(resolve));
      await rm(dir, { recursive: true, force: true });
    },
  };
}

const CONFIG = {
  access: {
    "mail-triage": {
      accounts: ["university"],
      actions: ["read", "mark", "archive", "note"],
      forward: { from: "gmail", to: ["Phone@Example.com"], perHour: 2 },
    },
    university: { accounts: ["university"] },
    main: { actions: ["read", "export"], send: { from: "gmail", to: ["@example.com"] } },
  },
  rules: [
    { agent: "mail-triage", accounts: ["university"] },
    { agent: "main", events: ["mail.auth", "mail.error"], enabled: true },
    { agent: "off", enabled: false },
  ],
};

describe("access", () => {
  test("an agent gets its entry, else *, else nothing", () => {
    assert.equal(accessFor(CONFIG, "shopping"), undefined);
    assert.deepEqual([...accessFor(CONFIG, "university")!.actions], ["read"]);
    assert.deepEqual(accessFor({ access: { "*": { accounts: ["gmail"] } } }, "shopping")!.accounts, ["gmail"]);
    assert.equal(accessFor(CONFIG, undefined), undefined);
  });

  test("allow lists are lower case and drop what is not an address", () => {
    assert.deepEqual(normalizeAllow(["A@B.com", "*@c.org", "@d.net", "nonsense", "a@b.com"]), ["a@b.com", "@c.org", "@d.net"]);
  });

  test("each action adds its tools", () => {
    const names = (agent: string) => mailTools(accessFor(CONFIG, agent)!, new MailClient("/x"), new RateLimiter(), quiet, "/ws").map((tool) => tool.name);
    assert.deepEqual(names("university"), ["mail_status", "mail_list", "mail_search", "mail_show", "mail_folders", "mail_attachments", "mail_notes"]);
    assert.ok(names("mail-triage").includes("mail_forward") && names("mail-triage").includes("mail_note"));
    assert.ok(!names("mail-triage").includes("mail_trash") && !names("mail-triage").includes("mail_send"));
    assert.ok(names("main").includes("mail_send") && names("main").includes("mail_export_attachment"));
  });
});

describe("tools against a fake daemon", () => {
  let daemon: Awaited<ReturnType<typeof fakeDaemon>>;
  before(async () => {
    daemon = await fakeDaemon((request) => {
      switch (request.op) {
        case "status":
          return { ok: true, accounts: [{ name: "university" }, { name: "gmail" }] };
        case "list":
          return { ok: true, count: 1, messages: [{ id: `${request.account || "university"}.1`, date: request.account === "gmail" ? "2026-10-02" : "2026-10-01" }] };
        case "show":
          return { ok: true, id: request.id, body: "x".repeat(50) };
        case "forward":
          return { ok: true, id: request.id, forwarded: true, to: request.to };
        case "note":
          return { ok: true, note: { mail_id: request.id, author: request.author, text: request.text } };
        case "export_attachment":
          return { ok: true, path: path.join(daemon.dir, "report.pdf"), filename: "report.pdf", mime_type: "application/pdf", size: 3 };
        default:
          return { ok: false, error: `unknown operation ${request.op}` };
      }
    });
    await writeFile(path.join(daemon.dir, "report.pdf"), "pdf");
  });
  after(() => daemon.close());

  const tools = (agent: string, limiter = new RateLimiter(), workspace?: string) =>
    Object.fromEntries(mailTools(accessFor(CONFIG, agent)!, new MailClient(daemon.socket), limiter, quiet, workspace).map((tool) => [tool.name, tool]));

  test("status and lists show only the agent's accounts", async () => {
    const triage = tools("mail-triage");
    assert.deepEqual((await triage.mail_status.execute("1", {})).details.accounts, [{ name: "university" }]);
    assert.equal(daemon.requests.at(-1).op, "status");
    await triage.mail_list.execute("2", {});
    assert.equal(daemon.requests.at(-1).account, "university");
    await assert.rejects(triage.mail_list.execute("3", { account: "gmail" }), /only the account\(s\) university/);
    await assert.rejects(triage.mail_show.execute("4", { mail_id: "gmail.1" }), /not mail of the account/);
    await assert.rejects(triage.mail_archive.execute("5", { mail_ids: ["university.1", "gmail.2"] }), /gmail\.2/);
  });

  test("an agent with every account merges nothing and lists them all", async () => {
    await tools("main").mail_list.execute("1", { unread: true, limit: 5 });
    const request = daemon.requests.at(-1);
    assert.equal(request.account, undefined);
    assert.equal(request.unread, true);
  });

  test("show cuts the body", async () => {
    const shown = (await tools("university").mail_show.execute("1", { mail_id: "university.1", max_chars: 10 })).details;
    assert.deepEqual([shown.body.length, shown.body_chars, shown.truncated], [10, 50, true]);
  });

  test("forward goes only to the allow list, and the hourly limit counts", async () => {
    const limiter = new RateLimiter();
    const triage = tools("mail-triage", limiter);
    await assert.rejects(triage.mail_forward.execute("1", { mail_id: "university.1", to: ["else@example.com"] }), /not allowed/);
    const done = (await triage.mail_forward.execute("2", { mail_id: "university.1", to: ["Me <phone@example.com>"] })).details;
    assert.deepEqual(done.to, ["phone@example.com"]);
    assert.equal(daemon.requests.at(-1).account, "gmail");
    await triage.mail_forward.execute("3", { mail_id: "university.1", to: ["phone@example.com"] });
    await assert.rejects(triage.mail_forward.execute("4", { mail_id: "university.1", to: ["phone@example.com"] }), /send limit/);
  });

  test("send allows a whole domain and refuses other domains", async () => {
    const main = tools("main");
    await assert.rejects(main.mail_send.execute("1", { to: ["a@other.org"], subject: "s", body: "b" }), /not allowed/);
    await assert.rejects(main.mail_send.execute("2", { to: ["a@example.com"], subject: "two\nlines", body: "b" }), /one line/);
  });

  test("notes carry the agent as author", async () => {
    const note = (await tools("mail-triage").mail_note.execute("1", { mail_id: "university.1", text: "kept" })).details;
    assert.deepEqual(note.note, { mail_id: "university.1", author: "mail-triage", text: "kept" });
  });

  test("export copies the attachment into the workspace", async () => {
    const workspace = await mkdtemp(path.join(os.tmpdir(), "uni-mail-ws-"));
    const saved = (await tools("main", new RateLimiter(), workspace).mail_export_attachment.execute("1", { mail_id: "gmail.1", index: 0 })).details;
    assert.equal(saved.path, path.join("mail-attachments", "gmail.1", "report.pdf"));
    assert.equal(await readFile(path.join(workspace, saved.path), "utf8"), "pdf");
    await rm(workspace, { recursive: true, force: true });
  });

  test("an aborted request closes at once", async () => {
    const slow = await fakeDaemon(() => undefined);
    const controller = new AbortController();
    const pending = new MailClient(slow.socket).request({ op: "events" }, 60_000, controller.signal);
    setTimeout(() => controller.abort(), 20);
    await assert.rejects(pending, /cancelled/);
    await slow.close();
  });

  test("a refusal of the daemon becomes the tool error", async () => {
    await assert.rejects(new MailClient(daemon.socket).request({ op: "nope" }), /unknown operation nope/);
    await assert.rejects(new MailClient(path.join(daemon.dir, "missing.sock")).request({ op: "status" }), /not running/);
  });
});

describe("rules", () => {
  test("disabled rules and rules without an agent drop out", () => {
    assert.deepEqual(parseRules(CONFIG).map((rule) => rule.agent), ["mail-triage", "main"]);
  });

  test("an event goes to the rules that match its type and account", () => {
    const rules = parseRules(CONFIG);
    const event = { event_id: "e1", event_type: "mail.new", account: "university", mail_id: "university.1", data: { subject: "secret" } };
    const [delivery] = deliveriesFor(event, rules);
    assert.equal(delivery.agent, "mail-triage");
    assert.ok(delivery.message.includes("university.1") && delivery.message.includes("e1"));
    assert.ok(!delivery.message.includes("secret"), "no header text in the message");
    assert.equal(DEFAULT_MESSAGE.includes("{mail_id}"), true);
    assert.deepEqual(deliveriesFor({ ...event, account: "gmail" }, rules), []);
    const auth = deliveriesFor({ event_id: "e2", event_type: "mail.auth", account: "gmail", mail_id: "", data: { detail: "sign in again" } }, rules);
    assert.deepEqual(auth.map((item) => item.agent), ["main"]);
    assert.ok(auth[0].message.includes("sign in again"));
  });
});

describe("Mail page", () => {
  let daemon: Awaited<ReturnType<typeof fakeDaemon>>;
  before(async () => {
    daemon = await fakeDaemon((request) => {
      switch (request.op) {
        case "status":
          return {
            ok: true,
            accounts: [
              { name: "university", provider: "microsoft", address: "me@uni.edu", auth: "oauth", status: "auth_required", error: "sign in again", last_sync: "2026-10-30T12:00:00+00:00", sync_days: 30, messages: 12, unread: 3, folders: ["INBOX"] },
              { name: "imap", provider: "imap", address: "me@example.com", auth: "password", status: "idle", error: "", last_sync: "", sync_days: 7, messages: 0, unread: 0 },
            ],
          };
        case "auth_begin":
          return request.account === "imap"
            ? { ok: false, error: "account imap uses a password, not OAuth" }
            : { ok: true, url: "https://login.example/authorize?state=s", redirect_uri: "https://login.example/nativeclient" };
        case "auth_finish":
          return { ok: true, account: request.account, message: "signed in; the account starts to sync in a few seconds" };
        case "notes":
          return { ok: true, count: 1, notes: [{ seq: 4, mail_id: "university.1", account: "university", author: "mail-triage", created: 1_790_000_000.5, text: "archived" }] };
        default:
          return { ok: false, error: `unknown operation ${request.op}` };
      }
    });
  });
  after(() => daemon.close());

  const delivery = { agent: "mail-triage", message: "secret text", timeoutSeconds: 900, eventId: "e1", eventType: "mail.new", account: "university", mailId: "university.1" };
  function fakeApi() {
    const opened: any[] = [];
    const resubmitted: string[] = [];
    const queue = {
      listPending: async () => [{ id: "e2:mail-triage", payload: { ...delivery, eventId: "e2" }, attempts: 1, receivedAt: 2000, updatedAt: 2000, lastError: "run r1 timeout" }],
      listClaims: async () => [{ id: "e3:mail-triage", payload: delivery, attempts: 0, receivedAt: 3000, updatedAt: 3000, claim: { token: "t", ownerId: "o", claimedAt: 3500 } }],
      listFailed: async () => [
        { id: "e1:mail-triage", payload: delivery, attempts: 3, receivedAt: 1000, updatedAt: 1000, failedAt: 5000, reason: "max-attempts", message: "AUTOMATION_FAILED no access" },
        { id: "e0:main", attempts: 3, receivedAt: 900, updatedAt: 900, failedAt: 6000, reason: "corrupt_payload" },
      ],
      resubmit: async (id: string) => {
        resubmitted.push(id);
        return id === "e1:mail-triage" ? { kind: "resubmitted" } : { kind: "not-found" };
      },
    };
    const actions: Record<string, any> = {};
    const api = {
      logger: quiet,
      runtime: { state: { openChannelIngressQueue: (options: any) => (opened.push(options), queue) } },
      session: { controls: { registerSessionAction: (action: any) => (actions[action.id] = action) } },
    };
    return { api, actions, opened, resubmitted };
  }
  function page(following = true) {
    const fake = fakeApi();
    registerPageOperations(fake.api, pageOperations(fake.api, () => new MailClient(daemon.socket), () => following));
    const call = (id: string, payload?: unknown) => fake.actions[id].handler({ pluginId: "uni-mail", actionId: id, payload, client: { scopes: ["operator.read"] } });
    return { ...fake, call };
  }

  test("queries need operator.read and actions operator.write", () => {
    const { actions } = page();
    const scopes = Object.fromEntries(Object.values(actions).map((action: any) => [action.id, action.requiredScopes]));
    assert.deepEqual(scopes, {
      accounts: ["operator.read"],
      "auth.begin": ["operator.write"],
      "auth.finish": ["operator.write"],
      queue: ["operator.read"],
      "queue.retry": ["operator.write"],
      notes: ["operator.read"],
    });
    for (const action of Object.values(actions) as any[]) assert.equal(action.schema.additionalProperties, false);
  });

  test("accounts show every account with its state", async () => {
    const { result } = await page().call("accounts", {});
    assert.deepEqual(result.accounts.map((account: any) => [account.name, account.auth, account.status, account.unread, account.messages]), [
      ["university", "oauth", "auth_required", 3, 12],
      ["imap", "password", "idle", 0, 0],
    ]);
    assert.equal(result.accounts[0].error, "sign in again");
    assert.equal(result.accounts[0].folders, undefined);
  });

  test("the sign-in starts and finishes at the daemon", async () => {
    const { call } = page();
    const begun = await call("auth.begin", { account: "university" });
    assert.deepEqual(begun.result, { account: "university", url: "https://login.example/authorize?state=s", redirectUri: "https://login.example/nativeclient" });
    assert.deepEqual(daemon.requests.at(-1), { op: "auth_begin", account: "university", send: false });
    const done = await call("auth.finish", { account: "university", redirect: "  https://login.example/nativeclient?code=c&state=s\n" });
    assert.equal(done.ok, true);
    assert.equal(daemon.requests.at(-1).redirect, "https://login.example/nativeclient?code=c&state=s");
    assert.deepEqual(await call("auth.begin", { account: "imap" }), { ok: false, error: "account imap uses a password, not OAuth", code: "UNI_MAIL" });
  });

  test("the runs come from the plugin's ingress queue, without the message text", async () => {
    const { call, opened } = page(false);
    const { result } = await call("queue", {});
    assert.deepEqual(opened, [{ accountId: "uni-mail" }]);
    assert.equal(result.following, false);
    assert.deepEqual(result.pending.map((run: any) => [run.id, run.attempts, run.lastError]), [["e2:mail-triage", 1, "run r1 timeout"]]);
    assert.deepEqual([result.running[0].agent, result.running[0].claimedAt], ["mail-triage", 3500]);
    assert.deepEqual(result.failed.map((run: any) => [run.id, run.retryable]), [["e0:main", false], ["e1:mail-triage", true]]);
    assert.deepEqual([result.failed[1].mailId, result.failed[1].lastError], ["university.1", "AUTOMATION_FAILED no access"]);
    assert.ok(!JSON.stringify(result).includes("secret text"));
  });

  test("retry queues a failed run again, and says why it cannot", async () => {
    const { call, resubmitted } = page();
    assert.deepEqual((await call("queue.retry", { id: "e1:mail-triage" })).result, { id: "e1:mail-triage", retried: true });
    const refused = await call("queue.retry", { id: "gone" });
    assert.equal(refused.ok, false);
    assert.match(refused.error, /no failed run/);
    assert.deepEqual(resubmitted, ["e1:mail-triage", "gone"]);
  });

  test("notes are the recent ones, with times in milliseconds", async () => {
    const { result } = await page().call("notes", { days: 30 });
    assert.deepEqual(daemon.requests.at(-1), { op: "notes", since: "30d", limit: 100 });
    assert.deepEqual(result.notes, [{ seq: 4, mailId: "university.1", account: "university", author: "mail-triage", created: 1_790_000_000_500, text: "archived" }]);
    await page().call("notes", undefined);
    assert.equal(daemon.requests.at(-1).since, "7d");
  });

  test("a stopped daemon is an error the page can show", async () => {
    const fake = fakeApi();
    registerPageOperations(fake.api, pageOperations(fake.api, () => new MailClient(path.join(daemon.dir, "missing.sock")), () => true));
    const answer = await fake.actions.accounts.handler({ payload: {} });
    assert.equal(answer.ok, false);
    assert.match(answer.error, /not running/);
  });

  test("the browser entry is declared and registers the page", async () => {
    const root = path.join(import.meta.dirname, "..");
    const manifest = JSON.parse(await readFile(path.join(root, "openclaw.plugin.json"), "utf8"));
    assert.match(manifest.controlUi.entry, /^dist\/(?:[\w-][\w.-]*\/)+[\w-][\w.-]*\.m?js$/);
    for (const style of manifest.controlUi.styles) await readFile(path.join(root, style));
    const { default: plugin } = await import(path.join(root, manifest.controlUi.entry));
    assert.equal(plugin.id, manifest.id);
    const registered: any[] = [];
    plugin.activate({ ui: { registerNavigation: (item: any) => registered.push(["nav", item.id, item.label, item.page.id]), registerPage: (item: any) => registered.push(["page", item.id, item.label]) } });
    assert.deepEqual(registered, [["nav", "mail", "Mail", "mail"], ["page", "mail", "Mail"]]);
  });
});
