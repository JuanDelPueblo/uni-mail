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
