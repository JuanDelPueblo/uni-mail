// The Mail page of uni-mail in the OpenClaw Control UI: the accounts and their
// sign-in, the new-mail runs, and recent notes.
//
// Hand-written browser JavaScript with no imports, so there is nothing to build
// or bundle: openclaw.plugin.json names this file as `controlUi.entry`. It
// calls the operations of src/page.ts through `plugins.sessionAction`, the
// transport that OpenClaw's feature contracts use (createFeatureClient sends
// the same request). The Gateway loads it only with Settings → Labs → Custom
// plugin UI (gateway.controlUi.experimental.customPlugins) turned on.

const PLUGIN_ID = "uni-mail";
const PAGE_ID = "mail";
const REFRESH_MS = 30_000;

/** An element with attributes, properties (`on*` handlers, `className`...) and children. Text is never parsed as HTML. */
function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on") || key === "className" || key === "value" || key === "checked" || key === "disabled") element[key] = value;
    else element.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

function operations(host) {
  return async (operation, input = {}) => {
    host.signal.throwIfAborted();
    const response = await host.request("plugins.sessionAction", { pluginId: PLUGIN_ID, actionId: operation, payload: input });
    host.signal.throwIfAborted();
    if (!response || response.ok !== true) {
      throw new Error(response && typeof response.error === "string" ? response.error : "the operation gave no answer");
    }
    return response.result;
  };
}

export default {
  id: PLUGIN_ID,
  activate(host) {
    host.ui.registerNavigation({ id: PAGE_ID, label: "Mail", icon: "mail", page: { id: PAGE_ID } });
    host.ui.registerPage({ id: PAGE_ID, label: "Mail", mount: mountPage });
  },
};

function mountPage(container, context) {
  const call = operations(context.host);
  const signal = context.signal;
  const locale = context.host.locale || undefined;
  const dateTime = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" });
  const relative = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  let presented = context.presented !== false;
  let noteDays = 7;
  let busy = false;

  const when = (ms) => {
    if (typeof ms !== "number" || !ms) return h("span", { className: "uni-mail-muted" }, "—");
    const minutes = Math.round((ms - Date.now()) / 60_000);
    const text = Math.abs(minutes) < 60
      ? relative.format(minutes, "minute")
      : Math.abs(minutes) < 48 * 60
        ? relative.format(Math.round(minutes / 60), "hour")
        : dateTime.format(new Date(ms));
    return h("time", { datetime: new Date(ms).toISOString(), title: dateTime.format(new Date(ms)) }, text);
  };
  const table = (headings, rows) =>
    h("div", { className: "uni-mail-scroll" },
      h("table", {}, h("thead", {}, h("tr", {}, headings.map((text) => h("th", { scope: "col" }, text)))), h("tbody", {}, rows)));
  const empty = (text) => h("p", { className: "uni-mail-muted" }, text);
  const errorLine = (error) => h("p", { className: "uni-mail-error", role: "alert" }, String(error?.message ?? error));
  const section = (title, ...rest) => {
    const body = h("div", { "aria-live": "polite" });
    const element = h("section", { className: "uni-mail-section" }, h("div", { className: "uni-mail-section-head" }, h("h2", {}, title), ...rest), body);
    return { element, body };
  };

  // Accounts, and the sign-in of one of them. The sign-in form sits outside the
  // table, so a refresh of the table keeps what was pasted.
  const accounts = section("Accounts");
  const signIn = h("div", { className: "uni-mail-signin", hidden: true });
  accounts.element.append(signIn);

  function renderAccounts({ accounts: list }) {
    if (!list.length) return accounts.body.replaceChildren(empty("The daemon has no accounts."));
    accounts.body.replaceChildren(table(
      ["Account", "State", "Last sync", "Unread / all", ""],
      list.map((account) => h("tr", {},
        h("td", {}, h("strong", {}, account.name), h("div", { className: "uni-mail-muted" }, [account.address, account.provider].filter(Boolean).join(" · "))),
        h("td", {},
          h("span", { className: `uni-mail-state uni-mail-state-${account.status === "idle" || account.status === "syncing" ? "ok" : account.status === "starting" ? "wait" : "bad"}` }, stateLabel(account.status)),
          account.error ? h("div", { className: "uni-mail-error-text" }, account.error) : null),
        h("td", {}, when(account.lastSync ? Date.parse(account.lastSync) : 0), account.syncDays ? h("div", { className: "uni-mail-muted" }, `keeps ${account.syncDays} days`) : null),
        h("td", { className: "uni-mail-number" }, `${account.unread} / ${account.messages}`),
        h("td", {}, account.auth === "password" ? null : h("button", { type: "button", onclick: () => openSignIn(account.name) }, "Sign in again")),
      )),
    ));
  }

  function stateLabel(status) {
    return { idle: "Synced", syncing: "Syncing", starting: "Starting", auth_required: "Sign-in needed", error: "Error" }[status] || status || "Unknown";
  }

  function openSignIn(name) {
    const send = h("input", { type: "checkbox" });
    const status = h("p", { "aria-live": "polite" });
    const start = h("button", { type: "button" }, "Get the sign-in link");
    const cancel = h("button", { type: "button", className: "uni-mail-secondary", onclick: closeSignIn }, "Cancel");
    const steps = h("div");
    start.onclick = async () => {
      start.disabled = true;
      status.replaceChildren("Starting the sign-in…");
      try {
        const begun = await call("auth.begin", { account: name, send: send.checked });
        if (signal.aborted) return;
        status.replaceChildren();
        start.hidden = true;
        send.disabled = true;
        steps.replaceChildren(finishForm(name, begun, status));
      } catch (error) {
        if (signal.aborted) return;
        status.replaceChildren(errorLine(error));
        start.disabled = false;
      }
    };
    signIn.replaceChildren(
      h("h3", {}, `Sign in to ${name} again`),
      h("label", { className: "uni-mail-check" }, send, " Also allow sending mail (needed once for Microsoft accounts that send)"),
      h("div", { className: "uni-mail-buttons" }, start, cancel),
      steps,
      status,
    );
    signIn.hidden = false;
    start.focus();
  }

  function finishForm(name, begun, status) {
    let link;
    try {
      const url = new URL(begun.url);
      link = url.protocol === "https:" ? h("a", { href: url.href, target: "_blank", rel: "noopener noreferrer" }, "Open the sign-in page") : null;
    } catch {
      link = null;
    }
    const redirect = h("input", { type: "text", inputmode: "url", placeholder: `${begun.redirectUri}/?code=…`, "aria-label": "The address the browser ended at", autocomplete: "off", spellcheck: "false" });
    const finish = h("button", { type: "submit" }, "Finish sign-in");
    const form = h("form", { className: "uni-mail-signin-form" },
      h("ol", {},
        h("li", {}, link ?? h("span", { className: "uni-mail-error-text" }, "The daemon sent a link that is not https; not shown."), " and sign in. The sign-in expires after 15 minutes."),
        h("li", {}, "The browser then goes to ", h("code", {}, `${begun.redirectUri}/?code=…`), ". That page does not load; this is expected."),
        h("li", {}, "Copy the full address from the address bar and paste it here:"),
      ),
      h("div", { className: "uni-mail-inline" }, redirect, finish),
    );
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (!redirect.value.trim()) return redirect.focus();
      finish.disabled = true;
      status.replaceChildren("Finishing the sign-in…");
      try {
        const done = await call("auth.finish", { account: name, redirect: redirect.value.trim() });
        if (signal.aborted) return;
        signIn.replaceChildren(h("p", { className: "uni-mail-ok", role: "status" }, `${name}: ${done.message}`), h("button", { type: "button", className: "uni-mail-secondary", onclick: closeSignIn }, "Close"));
        loadAccounts();
      } catch (error) {
        if (signal.aborted) return;
        status.replaceChildren(errorLine(error));
        finish.disabled = false;
      }
    };
    queueMicrotask(() => link?.focus());
    return form;
  }

  function closeSignIn() {
    signIn.hidden = true;
    signIn.replaceChildren();
  }

  // The new-mail runs of the ingress queue.
  const runs = section("New-mail runs");
  const runsStatus = h("p", { "aria-live": "polite" });
  runs.element.append(runsStatus);

  function renderRuns(queue) {
    const headings = ["Agent", "Account", "Mail ID", "Attempts", "Last error", "Received"];
    const cells = (run) => [
      h("td", {}, run.agent),
      h("td", {}, run.account),
      h("td", {}, h("code", {}, run.mailId || run.eventType || run.id)),
      h("td", { className: "uni-mail-number" }, String(run.attempts)),
      h("td", { className: "uni-mail-error-text" }, run.lastError),
      h("td", {}, when(run.receivedAt)),
    ];
    const group = (title, list, extraHeadings, extraCells) => [
      h("h3", {}, `${title} (${list.length})`),
      list.length ? table([...headings, ...extraHeadings], list.map((run) => h("tr", {}, cells(run), extraCells(run)))) : empty("None."),
    ];
    runs.body.replaceChildren(
      queue.following ? "" : h("p", { className: "uni-mail-warn" }, "No new-mail rule is set, so nothing follows new mail and a retried run does not start."),
      ...group("Running", queue.running, ["Started"], (run) => [h("td", {}, when(run.claimedAt))]),
      ...group("Queued", queue.pending, ["Last attempt"], (run) => [h("td", {}, when(run.lastAttemptAt))]),
      ...group("Failed", queue.failed, ["Failed", ""], (run) => [
        h("td", {}, when(run.failedAt)),
        h("td", {}, run.retryable ? h("button", { type: "button", onclick: (event) => retry(run, event.currentTarget) }, "Retry") : h("span", { className: "uni-mail-muted", title: "The queue kept no delivery for this run" }, "—")),
      ]),
    );
  }

  async function retry(run, button) {
    button.disabled = true;
    runsStatus.replaceChildren(`Queueing ${run.mailId || run.id} again…`);
    try {
      await call("queue.retry", { id: run.id });
      if (signal.aborted) return;
      runsStatus.replaceChildren(h("span", { className: "uni-mail-ok" }, `${run.mailId || run.id} is queued again for ${run.agent}; it starts within a minute.`));
      loadRuns();
    } catch (error) {
      if (signal.aborted) return;
      runsStatus.replaceChildren(errorLine(error));
      button.disabled = false;
    }
  }

  // Recent notes.
  const daysPicker = h("select", { "aria-label": "Notes of the last", onchange: () => { noteDays = Number(daysPicker.value); loadNotes(); } },
    [1, 7, 30, 90].map((days) => h("option", { value: String(days), selected: days === noteDays }, days === 1 ? "last day" : `last ${days} days`)));
  const notes = section("Recent notes", daysPicker);

  function renderNotes(result) {
    if (!result.notes.length) return notes.body.replaceChildren(empty(`No notes in the last ${result.days === 1 ? "day" : `${result.days} days`}.`));
    notes.body.replaceChildren(table(
      ["Time", "Mail ID", "Author", "Note"],
      result.notes.map((note) => h("tr", {},
        h("td", {}, when(note.created)),
        h("td", {}, h("code", {}, note.mailId)),
        h("td", {}, note.author || h("span", { className: "uni-mail-muted" }, "—")),
        h("td", { className: "uni-mail-note" }, note.text),
      )),
    ));
  }

  const load = (target, operation, input, render) => async () => {
    try {
      const result = await call(operation, input());
      if (!signal.aborted) render(result);
    } catch (error) {
      if (!signal.aborted) target.body.replaceChildren(errorLine(error));
    }
  };
  const loadAccounts = load(accounts, "accounts", () => ({}), renderAccounts);
  const loadRuns = load(runs, "queue", () => ({}), renderRuns);
  const loadNotes = load(notes, "notes", () => ({ days: noteDays }), renderNotes);

  const updated = h("span", { className: "uni-mail-muted" });
  const refreshButton = h("button", { type: "button", className: "uni-mail-secondary", onclick: () => refresh() }, "Refresh");
  async function refresh() {
    if (busy) return;
    busy = true;
    refreshButton.disabled = true;
    await Promise.all([loadAccounts(), loadRuns(), loadNotes()]);
    busy = false;
    if (signal.aborted) return;
    refreshButton.disabled = false;
    updated.textContent = `Updated ${new Date().toLocaleTimeString(locale)}`;
  }

  for (const target of [accounts, runs, notes]) target.body.replaceChildren(empty("Loading…"));
  const page = h("div", { className: "uni-mail-page" },
    h("header", { className: "uni-mail-header" }, h("h1", {}, "Mail"), h("div", { className: "uni-mail-inline" }, updated, refreshButton)),
    accounts.element,
    runs.element,
    notes.element,
  );
  container.append(page);
  refresh();

  // Keep the page current while it is on screen; nothing runs while it is hidden.
  const timer = setInterval(() => {
    if (presented && document.visibilityState === "visible" && context.host.connection?.connected !== false) refresh();
  }, REFRESH_MS);
  signal.addEventListener("abort", () => clearInterval(timer), { once: true });

  return {
    update(next) {
      const wasPresented = presented;
      presented = next.presented !== false;
      if (presented && !wasPresented) refresh();
    },
    dispose() {
      clearInterval(timer);
      page.remove();
    },
  };
}
