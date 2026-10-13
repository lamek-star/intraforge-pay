"use strict";
(async () => {
  const { api, el, show, fmtTime, openReceipt } = IFPay;
  const me = await IFPay.requireUser(["admin", "supervisor"]);
  if (!me) return;
  const $ = (id) => document.getElementById(id);
  const isAdmin = me.role === "admin"; // UI convenience only; the server checks capabilities on every request
  document.querySelectorAll("[data-cap]").forEach((n) => n.classList.toggle("hidden", !isAdmin));
  const err = (e) => show("page-msg", e.message, "bad");
  const badge = (text, kind) => el("span", { class: `badge ${kind || ""}` }, text);

  const loaders = {
    async overview() {
      const o = await api("GET", "/api/admin/overview");
      const c = o.conservation;
      $("ov-issued").textContent = c.issued;
      $("ov-customers").textContent = c.customer_liability;
      $("ov-merchant").textContent = c.merchant_payable;
      $("ov-ok").replaceChildren(badge(c.ok ? "✓ Balanced" : "✗ FAILED", c.ok ? "ok" : "bad"));
      $("ov-detail").textContent = `Debits ${c.total_debits} = credits ${c.total_credits}; issued ${c.issued} = customers ${c.customer_liability} + shop ${c.merchant_payable}; ${c.unbalanced_transactions} unbalanced transactions; ${c.negative_wallets} negative wallets.`;
      $("ov-accounts").replaceChildren(...o.accounts.map((a) => el("tr", {},
        el("td", {}, el("div", {}, a.name), el("div", { class: "mono" }, a.code)), el("td", { class: "hide-sm" }, a.kind),
        el("td", { class: "num" }, a.debits), el("td", { class: "num" }, a.credits), el("td", { class: "num" }, el("strong", {}, a.balance)))));
    },
    async credits() {
      const [customers, allocations] = await Promise.all([api("GET", "/api/admin/customers"), api("GET", "/api/admin/allocations")]);
      $("alloc-customer").replaceChildren(...customers.filter((c) => c.active).map((c) =>
        el("option", { value: c.id }, `${c.display_name} (${c.username}) — ${c.balance}`)));
      $("alloc-all").onclick = async () => {
        const pendingFor = new Set(allocations.filter((a) => a.status !== "rejected").map((a) => a.customer_username));
        const targets = customers.filter((c) => c.active && c.balance === "0.00" && !pendingFor.has(c.username));
        try {
          for (const c of targets) await api("POST", "/api/admin/allocations", { customer_id: c.id, amount: "100", reason: "Pilot opening allocation" });
          show("page-msg", `${targets.length} allocation request(s) created. A checker must now approve them.`, "ok");
          loaders.credits();
        } catch (e) { err(e); }
      };
      $("alloc-list").replaceChildren(...allocations.map((a) => el("tr", {},
        el("td", {}, a.customer, el("div", { class: "small muted" }, a.reason)),
        el("td", { class: "num" }, a.amount),
        el("td", {}, badge(a.status, { approved: "ok", rejected: "bad", pending: "warn" }[a.status])),
        el("td", { class: "hide-sm small" }, `${a.requested_by} → ${a.decided_by || "…"}`),
        el("td", {}, a.can_decide ? el("div", { class: "row" },
          el("button", { onclick: () => decide(a.id, "approve") }, "Approve"),
          el("button", { class: "secondary", onclick: () => decide(a.id, "reject") }, "Reject"))
          : a.status === "pending" ? el("span", { class: "small muted" }, "awaiting another checker") : ""))));
    },
    async customers() {
      const customers = await api("GET", "/api/admin/customers");
      $("cust-list").replaceChildren(...customers.map((c) => el("tr", {},
        el("td", { class: "mono" }, c.username), el("td", {}, c.display_name), el("td", { class: "num" }, c.balance),
        el("td", {}, badge(c.active ? "active" : "deactivated", c.active ? "ok" : "bad")))));
    },
    async transactions() {
      const txs = await api("GET", "/api/admin/transactions");
      $("tx-list").replaceChildren(...txs.map((t) => el("tr", {},
        el("td", {}, t.seq), el("td", { class: "small" }, fmtTime(t.created_at), el("div", { class: "muted" }, t.created_by)),
        el("td", {}, badge(t.kind, t.kind === "purchase" ? "info" : "")),
        el("td", { class: "small" }, t.entries.map((e) => el("div", {}, `${e.side === "debit" ? "Dr" : "Cr"} ${e.amount} `, el("span", { class: "mono" }, e.account)))),
        el("td", {}, t.receipt_number ? el("button", { class: "link", onclick: () => openReceipt(t.receipt_number).catch(err) }, t.receipt_number) : ""))));
    },
    async audit() {
      const action = $("audit-filter").value;
      const events = await api("GET", `/api/admin/audit${action ? `?action=${encodeURIComponent(action)}` : ""}`);
      $("audit-list").replaceChildren(...events.map((e) => el("tr", {},
        el("td", { class: "small" }, fmtTime(e.at)), el("td", {}, e.actor), el("td", { class: "mono" }, e.action),
        el("td", {}, badge(e.result, { success: "ok", denied: "warn", error: "bad" }[e.result])),
        el("td", { class: "small" }, e.subject_type ? `${e.subject_type} ${e.subject_id}` : "",
          Object.keys(e.detail).length ? el("div", { class: "mono" }, JSON.stringify(e.detail)) : ""))));
    },
    async integration() {
      const i = await api("GET", "/api/admin/integration");
      const s = i.status;
      $("if-status").replaceChildren(s ? badge(s.ok ? "✓ Verified" : "✗ Not verified", s.ok ? "ok" : "bad") : badge("not checked yet", "warn"));
      $("if-status").className = "";
      $("if-checked").textContent = s ? `checked ${fmtTime(s.checked_at)}` : "";
      $("if-detail").textContent = s && !s.ok ? `${s.detail.code}: ${s.detail.message}` : (i.config ? `Application "${i.config.application_name}" (${i.config.application_id}) · bucket "${i.config.bucket_name}" at ${i.config.base_url}` : "Not configured yet.");
      $("if-cred").replaceChildren(badge(i.credential_file_present ? "present" : "missing", i.credential_file_present ? "ok" : "bad"));
      $("if-cred").className = "";
      if (i.config && !$("if-json").value) $("if-json").value = JSON.stringify(i.config, null, 2);
    },
    async users() {
      const users = await api("GET", "/api/admin/users");
      $("user-list").replaceChildren(...users.map((u) => el("tr", {},
        el("td", { class: "mono" }, u.username), el("td", {}, u.display_name), el("td", {}, u.role),
        el("td", {}, badge(u.active ? "active" : "deactivated", u.active ? "ok" : "bad")),
        el("td", {}, u.active && u.id !== me.id ? el("button", { class: "secondary", onclick: () => deactivate(u) }, "Deactivate") : ""))));
    },
  };

  async function decide(id, verb) {
    try {
      await api("POST", `/api/admin/allocations/${id}/${verb}`, { note: "" });
      show("page-msg", verb === "approve" ? "Approved: the credits were posted to the journal." : "Rejected: nothing was posted.", "ok");
      loaders.credits();
    } catch (e) { err(e); }
  }

  async function deactivate(user) {
    if (!window.confirm(`Deactivate ${user.username}? They are signed out immediately.`)) return;
    try { await api("POST", `/api/admin/users/${user.id}/deactivate`); loaders.users(); } catch (e) { err(e); }
  }

  function select(tab) {
    document.querySelectorAll("#tabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    document.querySelectorAll("[data-panel]").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== tab));
    show("page-msg", "");
    loaders[tab]().catch(err);
  }
  document.querySelectorAll("#tabs [role=tab]").forEach((b) => b.addEventListener("click", () => select(b.dataset.tab)));

  $("alloc-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("POST", "/api/admin/allocations", { customer_id: $("alloc-customer").value, amount: $("alloc-amount").value, reason: $("alloc-reason").value });
      show("page-msg", "Requested. Another administrator or the supervisor must approve it.", "ok");
      loaders.credits();
    } catch (e) { err(e); }
  });
  $("cust-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("POST", "/api/admin/customers", { username: $("cust-username").value, display_name: $("cust-name").value, password: $("cust-password").value });
      event.target.reset();
      show("page-msg", "Customer created with an empty wallet. Allocate credits under Credits.", "ok");
      loaders.customers();
    } catch (e) { err(e); }
  });
  $("audit-filter").addEventListener("change", () => loaders.audit().catch(err));
  $("audit-refresh").addEventListener("click", () => loaders.audit().catch(err));
  $("if-file").addEventListener("change", async () => {
    const file = $("if-file").files[0];
    if (file) $("if-json").value = await file.text();
  });
  $("if-save").addEventListener("click", async () => {
    let body;
    try { body = JSON.parse($("if-json").value); } catch (_) { show("page-msg", "That is not valid JSON.", "bad"); return; }
    try {
      const r = await api("PUT", "/api/admin/integration", body);
      show("page-msg", r.status.ok ? "Saved and verified with IntraForge." : `Saved, but verification failed: ${r.status.message}`, r.status.ok ? "ok" : "bad");
      loaders.integration();
    } catch (e) { err(e); }
  });
  $("if-verify").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/admin/integration/verify");
      show("page-msg", r.ok ? "Verified: the credential is valid for this application and bucket." : `Not verified: ${r.message}`, r.ok ? "ok" : "bad");
      loaders.integration();
    } catch (e) { err(e); }
  });

  select("overview");
})();
