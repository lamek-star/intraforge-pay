"use strict";
(async () => {
  const { api, el, show, fmtTime, openReceipt } = IFPay;
  if (!(await IFPay.requireUser("staff", "checkout.create"))) return;
  const $ = (id) => document.getElementById(id);
  let current = null;
  let poll = null;
  const COUNTER_KEY = "ifpay-counter";
  const zone = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (_) { return ""; } })();
  const myCounter = () => { try { return localStorage.getItem(COUNTER_KEY) || ""; } catch (_) { return ""; } };

  async function loadCounters() {
    const list = await api("GET", "api/shop/counters?all=true");
    const active = list.filter((c) => c.active);
    if (myCounter() && !active.some((c) => c.id === myCounter())) { try { localStorage.removeItem(COUNTER_KEY); } catch (_) {} }
    $("counter").replaceChildren(el("option", { value: "" }, "No counter"),
      ...active.map((c) => el("option", { value: c.id }, c.name)));
    $("counter").value = myCounter();
    $("counter-row").classList.toggle("hidden", active.length === 0);
    if (IFPay.can("users.manage")) {
      $("counters-admin").classList.remove("hidden");
      $("counters-list").replaceChildren(...list.map((c) => el("li", {},
        el("span", { class: "grow" }, c.name, c.active ? "" : " (not in use)"),
        el("button", { class: "secondary", type: "button", onclick: async () => {
          try { await api("POST", `api/shop/counters/${c.id}/${c.active ? "disable" : "enable"}`); await loadCounters(); await refresh(); }
          catch (e) { show("counter-msg", e.message, "bad"); }
        } }, c.active ? "Stop using" : "Use again"))));
    }
  }

  async function refresh() {
    const params = new URLSearchParams();
    if (zone) params.set("tz", zone);
    if (myCounter()) params.set("counter", myCounter());
    const s = await api("GET", `api/shop?${params}`);
    $("shop-name").textContent = s.merchant.name;
    document.title = `${s.merchant.name} Checkout`;
    $("total-sales").replaceChildren(s.total_simulated_sales, el("small", {}, " IF Credits"));
    $("today-total").replaceChildren(s.counter_total ?? s.today_total, el("small", {}, s.counter_total ? " IF Credits at this counter" : " IF Credits"));
    const several = s.counters_today.length > 1 || (s.counters_today.length === 1 && s.counters_today[0].counter_id);
    $("counters-today").classList.toggle("hidden", !several);
    $("counters-today").replaceChildren(...s.counters_today.map((c) => el("li", {},
      el("span", { class: "grow" }, c.counter, el("span", { class: "small muted" }, ` · ${c.sales} ${c.sales === 1 ? "sale" : "sales"}`)),
      el("span", { class: "amount pos" }, c.total))),
      ...(several ? [el("li", {}, el("strong", { class: "grow" }, "All counters"), el("strong", {}, s.today_total))] : []));
    $("verified-line").textContent = s.intraforge.ok
      ? `Shop verified with IntraForge (checked ${fmtTime(s.intraforge.checked_at)}).`
      : "Shop not yet verified with IntraForge — it is checked when you generate a QR.";
    $("today-list").replaceChildren(...s.today.map((t) => el("li", {},
      el("div", { class: "grow" },
        el("div", {}, el("strong", {}, t.receipt_number), " · ", t.customer, t.counter ? ` · ${t.counter}` : ""),
        el("div", { class: "small muted" }, fmtTime(t.issued_at), " · ",
          t.uploaded ? el("span", { class: "badge ok" }, "stored in IntraForge") : el("span", { class: "badge warn" }, "not stored yet"))),
      el("span", { class: "amount pos" }, `+${t.amount}`),
      el("button", { class: "secondary", onclick: () => viewReceipt(t.receipt_number) }, "Receipt"))));
    $("today-empty").classList.toggle("hidden", s.today.length > 0);
  }

  async function loadRefunds() {
    const rows = await api("GET", "api/shop/refunds");
    const tone = { approved: "ok", rejected: "bad", pending: "warn" };
    $("refund-list").replaceChildren(...rows.slice(0, 20).map((r) => el("li", {},
      el("div", { class: "grow" },
        el("div", {}, el("strong", {}, r.receipt_number), " · ", r.customer),
        el("div", { class: "small muted" }, fmtTime(r.requested_at), " · ", r.reason,
          r.status === "approved" ? ` · ${r.returned_to_wallet} returned` : "")),
      el("span", { class: "amount" }, r.amount), el("span", { class: `badge ${tone[r.status]}` }, r.status))));
  }

  function viewReceipt(number) {
    openReceipt(number, { onUpload: uploadReceipt, onAttach: attachDocument }).catch((e) => show("shop-msg", e.message, "bad"));
  }

  async function uploadReceipt(number) {
    try {
      const r = await api("POST", `api/shop/receipts/${encodeURIComponent(number)}/upload`);
      await openReceipt(number, { onUpload: uploadReceipt, onAttach: attachDocument });
      show("receipt-msg", `Stored in IntraForge as ${r.filename}.`, "ok");
      refresh();
    } catch (e) { show("receipt-msg", e.message, "bad"); }
  }

  function attachDocument(number) {
    const input = $("doc-input");
    input.value = "";
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) return;
      try {
        const r = await api("POST", `api/shop/receipts/${encodeURIComponent(number)}/documents`, file,
          { "Content-Type": file.type || "application/octet-stream", "X-Filename": file.name });
        await openReceipt(number, { onUpload: uploadReceipt, onAttach: attachDocument });
        show("receipt-msg", `Supporting document stored in IntraForge as ${r.filename}.`, "ok");
      } catch (e) { show("receipt-msg", e.message, "bad"); }
    };
    input.click();
  }

  function setStatus(info) {
    const badge = $("qr-status");
    const map = { pending: ["waiting for customer", "info"], paid: ["PAID", "ok"], expired: ["expired", "bad"], cancelled: ["cancelled", "warn"] };
    const [text, kind] = map[info.status] || [info.status, ""];
    badge.textContent = text;
    badge.className = `badge ${kind}`;
    $("qr-expiry").textContent = info.status === "pending" ? `expires ${new Date(info.expires_at).toLocaleTimeString()}` : "";
    const done = info.status !== "pending";
    $("cancel-qr").classList.toggle("hidden", done);
    $("new-qr").classList.toggle("hidden", !done);
    $("view-receipt").classList.toggle("hidden", info.status !== "paid");
    $("qr-img").classList.toggle("hidden", done);
    if (done && poll) { clearInterval(poll); poll = null; refresh(); }
  }

  $("amount-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    show("shop-msg", "");
    $("generate").disabled = true;
    try {
      current = await api("POST", "api/shop/payment-requests", { amount: $("amount").value.trim(), counter_id: myCounter() || null });
      $("qr-img").src = current.qr_svg;
      $("qr-amount").textContent = `${current.amount} IF Credits`;
      $("qr-code").textContent = current.code;
      $("qr-card").classList.remove("hidden");
      setStatus(current);
      if (poll) clearInterval(poll);
      poll = setInterval(async () => {
        try { const info = await api("GET", `api/shop/payment-requests/${current.id}`); current = { ...current, ...info }; setStatus(current); }
        catch (_) { /* keep polling */ }
      }, 2000);
      $("qr-card").scrollIntoView({ behavior: "smooth" });
    } catch (e) {
      show("shop-msg", e.message, "bad");
    } finally {
      $("generate").disabled = false;
    }
  });

  $("counter").addEventListener("change", async () => {
    try { if ($("counter").value) localStorage.setItem(COUNTER_KEY, $("counter").value); else localStorage.removeItem(COUNTER_KEY); } catch (_) {}
    await refresh();
  });

  $("counter-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    show("counter-msg", "");
    try {
      await api("POST", "api/shop/counters", { name: $("counter-name").value });
      $("counter-name").value = "";
      await loadCounters();
    } catch (e) { show("counter-msg", e.message, "bad"); }
  });

  $("refund-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("POST", "api/shop/refunds", { receipt_number: $("refund-receipt").value.trim(),
        amount: $("refund-amount").value.trim(), reason: $("refund-reason").value.trim() });
      event.target.reset();
      show("refund-msg", "Refund requested. A supervisor must approve it before any credits move.", "ok");
      loadRefunds();
    } catch (e) { show("refund-msg", e.message, "bad"); }
  });
  loadRefunds().catch(() => {});

  $("cancel-qr").addEventListener("click", async () => {
    try { setStatus(await api("POST", `api/shop/payment-requests/${current.id}/cancel`)); } catch (e) { show("shop-msg", e.message, "bad"); }
  });
  $("new-qr").addEventListener("click", () => { $("qr-card").classList.add("hidden"); $("amount").value = ""; $("amount").focus(); });
  $("view-receipt").addEventListener("click", () => current.receipt_number && viewReceipt(current.receipt_number));
  $("copy-code").addEventListener("click", () => navigator.clipboard && navigator.clipboard.writeText(current.code));

  $("pair-btn").addEventListener("click", async () => {
    show("pair-msg", "");
    try {
      const p = await api("GET", "api/shop/pairing");
      $("pair-img").src = p.qr_svg;
      $("app-img").src = p.app_qr_svg;
      $("app-link").href = p.app_url;
      $("pair-shop").textContent = p.shop;
      $("pair-code").textContent = p.code;
      $("pair-box").classList.remove("hidden");
      $("pair-btn").classList.add("hidden");
    } catch (e) { show("pair-msg", e.message, "bad"); }
  });

  await loadCounters().catch(() => {});
  await refresh();
  setInterval(() => refresh().catch(() => {}), 15000);
})();
