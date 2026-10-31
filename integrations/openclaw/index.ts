// uni-mail for OpenClaw. Mail tools scoped per agent (`access`), and one agent
// run for each new mail that a rule matches (`rules`). The daemon keeps the
// accounts, their sign-in and the index; this plugin only talks to its socket.

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { accessFor, RateLimiter } from "./src/access.ts";
import { MailClient, resolveSocket } from "./src/client.ts";
import { inboundService, parseRules } from "./src/inbound.ts";
import { mailTools } from "./src/tools.ts";

export default definePluginEntry({
  id: "uni-mail",
  name: "uni-mail",
  description: "Mail tools from the uni-mail daemon, scoped per agent, and agent runs for new mail.",
  register(api: any) {
    const config = () => api.pluginConfig ?? {};
    const client = () => new MailClient(resolveSocket(config().socket));
    const limiter = new RateLimiter();

    api.registerTool((context: any) => {
      const access = accessFor(config(), context.agentId);
      if (!access) return null;
      return mailTools(access, client(), limiter, api.logger, context.workspaceDir);
    });

    if (api.registrationMode !== "full") return;
    api.registerService(inboundService(api, client, () => parseRules(config())));
  },
});
