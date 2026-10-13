// Shared helpers: API calls with the CSRF header, safe DOM building (no innerHTML with data), receipt viewer.
"use strict";

const IFPay = (() => {
  let csrf = null;
  let me = null;

  async function api(method, path, body, extraHeaders) {
    const headers = { Accept: "application/json", ...(extraHeaders || {}) };
    let payload;
    if (body instanceof Blob || body instanceof ArrayBuffer) {
      payload = body;
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    if (method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
    const res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (res.status === 401 && !path.startsWith("/api/auth/login")) {
      location.href = "/login";
      throw new Error("Please sign in.");
    }
    if (!res.ok) {
      const err = new Error((data && data.message) || `Request failed (${res.status}).`);
      err.status = res.status; err.code = data && data.code;
      throw err;
    }
    return data;
  }

  async function requireUser(allowedRoles) {
    const data = await api("GET", "/api/me");
    csrf = data.csrf_token; me = data.user;
    if (allowedRoles && !allowedRoles.includes(me.role)) { location.href = data.home; return null; }
    const who = document.getElementById("who");
    if (who) who.textContent = `${me.display_name} · ${me.role}`;
    const out = document.getElementById("logout");
    if (out) out.addEventListener("click", async () => { await api("POST", "/api/auth/logout"); location.href = "/login"; });
    return me;
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function show(id, message, kind) {
    const box = document.getElementById(id);
    if (!box) return;
    box.className = `msg ${kind || "info"}`;
    box.textContent = message;
    box.classList.toggle("hidden", !message);
  }

  const fmtTime = (iso) => new Date(iso).toLocaleString();

  function receiptDialog() {
    let dlg = document.getElementById("receipt-dialog");
    if (!dlg) {
      dlg = el("dialog", { id: "receipt-dialog", "aria-labelledby": "receipt-title" });
      document.body.append(dlg);
    }
    return dlg;
  }

  async function openReceipt(number, opts) {
    const r = await api("GET", `/api/receipts/${encodeURIComponent(number)}`);
    const b = r.body;
    const dlg = receiptDialog();
    dlg.replaceChildren();
    const uploads = r.uploads.length
      ? el("ul", { class: "small" }, r.uploads.map((u) =>
          el("li", {}, `${u.kind === "receipt" ? "Receipt" : "Supporting document"} stored in IntraForge bucket: `,
             el("span", { class: "mono" }, u.filename), ` (file ${u.intraforge_file_id})`)))
      : el("p", { class: "small muted" }, "Not stored in the IntraForge bucket yet — the shop server stores it automatically right after payment (reopen to refresh), or the shop can upload it.");
    const body = el("div", { class: "dialog-body receipt" },
      el("div", { class: "sim" }, b.notice),
      el("h2", { id: "receipt-title", class: "mt" }, "Receipt ", b.receipt_number),
      el("dl", {},
        el("dt", {}, "Shop"), el("dd", {}, b.merchant.name),
        el("dt", {}, "Customer"), el("dd", {}, b.customer.name),
        el("dt", {}, "Amount"), el("dd", { class: "amount" }, `${b.amount} IF Credits (simulated)`),
        el("dt", {}, "Issued"), el("dd", {}, `${fmtTime(b.issued_at)} (${b.issued_at} UTC)`),
        el("dt", {}, "Journal ref"), el("dd", { class: "mono" }, b.journal_reference),
        el("dt", {}, "Transaction"), el("dd", { class: "mono" }, b.journal_transaction_id)),
      el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "Side"), el("th", {}, "Account"), el("th", { class: "num" }, "Amount"))),
        el("tbody", {}, b.journal_entries.map((e) => el("tr", {}, el("td", {}, e.side), el("td", { class: "mono" }, e.account), el("td", { class: "num" }, e.amount))))),
      el("p", { class: "small muted" }, "Receipt fingerprint (SHA-256) — identical for the shop and the customer:"),
      el("p", { class: "mono" }, r.sha256),
      uploads,
      el("div", { class: "sim mt" }, b.notice),
      el("div", { class: "row no-print mt" },
        opts && opts.onUpload ? el("button", { class: "secondary", onclick: () => opts.onUpload(b.receipt_number, dlg) }, "Upload receipt to IntraForge") : null,
        opts && opts.onAttach ? el("button", { class: "secondary", onclick: () => opts.onAttach(b.receipt_number, dlg) }, "Attach supporting document") : null,
        el("button", { class: "secondary", onclick: () => window.print() }, "Print"),
        el("button", { onclick: () => dlg.close() }, "Close")),
      el("div", { id: "receipt-msg", class: "msg hidden" }));
    dlg.append(body);
    if (!dlg.open) dlg.showModal();
  }

  return { api, requireUser, el, show, fmtTime, openReceipt, get me() { return me; } };
})();
