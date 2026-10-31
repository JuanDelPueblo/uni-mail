// A client of the uni-maild socket: one JSON request and a newline, one JSON
// response and a newline, then the daemon closes the connection.

import { readFileSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const RESPONSE_LIMIT = 64 * 1024 * 1024;
const DEFAULT_SOCKET = "/run/uni-mail/mail.sock";

export class MailError extends Error {
  readonly response?: Record<string, unknown>;

  constructor(message: string, response?: Record<string, unknown>) {
    super(message);
    this.name = "MailError";
    this.response = response;
  }
}

/** The socket: the plugin setting, $UNI_MAIL_SOCKET, the user's uni-mail config, or the system socket. */
export function resolveSocket(configured?: string): string {
  if (configured) return configured;
  if (process.env.UNI_MAIL_SOCKET) return process.env.UNI_MAIL_SOCKET;
  const configHome = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config");
  for (const file of [process.env.UNI_MAIL_CONFIG, path.join(configHome, "uni-mail", "config.json"), "/etc/uni-mail/config.json"]) {
    if (!file) continue;
    try {
      const socket = JSON.parse(readFileSync(file, "utf8")).socket;
      if (typeof socket === "string" && socket) return socket;
    } catch {
      // A missing or unreadable config names no socket.
    }
  }
  return DEFAULT_SOCKET;
}

export type MailResponse = Record<string, any>;

export class MailClient {
  readonly socketPath: string;
  readonly timeoutMs: number;

  constructor(socketPath: string, timeoutMs = 120_000) {
    this.socketPath = socketPath;
    this.timeoutMs = timeoutMs;
  }

  request(payload: Record<string, unknown>, timeoutMs = this.timeoutMs): Promise<MailResponse> {
    return new Promise((resolve, reject) => {
      const connection = net.createConnection(this.socketPath);
      const chunks: Buffer[] = [];
      let total = 0;
      let settled = false;
      const finish = (error: Error | null, value?: MailResponse) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        connection.destroy();
        if (error) reject(error);
        else resolve(value!);
      };
      const timer = setTimeout(
        () => finish(new MailError(`the mail service did not answer in ${Math.round(timeoutMs / 1000)} s`)),
        timeoutMs,
      );
      connection.on("connect", () => connection.write(`${JSON.stringify(payload)}\n`));
      connection.on("data", (chunk: Buffer) => {
        chunks.push(chunk);
        total += chunk.length;
        if (total > RESPONSE_LIMIT) finish(new MailError("the mail service response is too large"));
      });
      connection.on("end", () => {
        if (!total) return finish(new MailError("the mail service closed the connection without an answer"));
        let response: MailResponse;
        try {
          response = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        } catch {
          return finish(new MailError("the mail service sent a response that is not JSON"));
        }
        if (!response.ok) return finish(new MailError(String(response.error || "the mail request failed"), response));
        delete response.ok;
        finish(null, response);
      });
      connection.on("error", (error: NodeJS.ErrnoException) => {
        if (error.code === "ENOENT" || error.code === "ECONNREFUSED") {
          finish(new MailError(`the mail service is not running (no listener on ${this.socketPath})`));
        } else if (error.code === "EACCES") {
          finish(new MailError(`no permission to use the mail service socket ${this.socketPath}`));
        } else {
          finish(new MailError(`the mail service connection failed: ${error.message}`));
        }
      });
    });
  }
}
