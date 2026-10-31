// uni-mail for OpenClaw. Mail tools scoped per agent (`access`), and one agent
// run for each new mail that a rule matches (`rules`). The daemon keeps the
// accounts, their sign-in and the index; this plugin only talks to its socket.
// The Mail page of the Control UI (dist/control-ui/) shows the accounts, the
// new-mail runs and recent notes, through the operations of src/page.ts.

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { accessFor, RateLimiter } from "./src/access.ts";
import { MailClient, resolveSocket } from "./src/client.ts";
import { inboundService, parseRules } from "./src/inbound.ts";
import { pageOperations, registerPageOperations } from "./src/page.ts";
import { mailTools } from "./src/tools.ts";

export default definePluginEntry({
  id: "uni-mail",
  name: "uni-mail",
  description: "Mail tools from the uni-mail daemon, scoped per agent, agent runs for new mail, and a Mail page in the Control UI.",
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
    const rules = () => parseRules(config());
    api.registerService(inboundService(api, client, rules));
    registerPageOperations(api, pageOperations(api, client, () => rules().length > 0));
  },
});
