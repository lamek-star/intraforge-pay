"use strict";
(async () => {
  const { api, el, show, fmtTime, openReceipt } = IFPay;
  if (!(await IFPay.requireUser(["cashier"]))) return;
  const $ = (id) => document.getElementById(id);
  let current = null;
  let poll = null;

  async function refresh() {
    const s = await api("GET", "/api/shop");
    $("shop-name").textContent = s.merchant.name;
    document.title = `${s.merchant.name} Checkout`;
    $("total-sales").replaceChildren(s.total_simulated_sales, el("small", {}, " IF Credits"));
    $("today-total").replaceChildren(s.today_total, el("small", {}, " IF Credits"));
    $("verified-line").textContent = s.intraforge.ok
      ? `Shop verified with IntraForge (checked ${fmtTime(s.intraforge.checked_at)}).`
      : "Shop not yet verified with IntraForge — it is checked when you generate a QR.";
    $("today-list").replaceChildren(...s.today.map((t) => el("li", {},
      el("div", { class: "grow" },
        el("div", {}, el("strong", {}, t.receipt_number), " · ", t.customer),
        el("div", { class: "small muted" }, fmtTime(t.issued_at), " · ",
          t.uploaded ? el("span", { class: "badge ok" }, "stored in IntraForge") : el("span", { class: "badge warn" }, "not stored yet"))),
      el("span", { class: "amount pos" }, `+${t.amount}`),
      el("button", { class: "secondary", onclick: () => viewReceipt(t.receipt_number) }, "Receipt"))));
    $("today-empty").classList.toggle("hidden", s.today.length > 0);
  }

  function viewReceipt(number) {
    openReceipt(number, { onUpload: uploadReceipt, onAttach: attachDocument }).catch((e) => show("shop-msg", e.message, "bad"));
  }

  async function uploadReceipt(number) {
    try {
      const r = await api("POST", `/api/shop/receipts/${encodeURIComponent(number)}/upload`);
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
        const r = await api("POST", `/api/shop/receipts/${encodeURIComponent(number)}/documents`, file,
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
      current = await api("POST", "/api/shop/payment-requests", { amount: $("amount").value.trim() });
      $("qr-img").src = current.qr_svg;
      $("qr-amount").textContent = `${current.amount} IF Credits`;
      $("qr-code").textContent = current.code;
      $("qr-card").classList.remove("hidden");
      setStatus(current);
      if (poll) clearInterval(poll);
      poll = setInterval(async () => {
        try { const info = await api("GET", `/api/shop/payment-requests/${current.id}`); current = { ...current, ...info }; setStatus(current); }
        catch (_) { /* keep polling */ }
      }, 2000);
      $("qr-card").scrollIntoView({ behavior: "smooth" });
    } catch (e) {
      show("shop-msg", e.message, "bad");
    } finally {
      $("generate").disabled = false;
    }
  });

  $("cancel-qr").addEventListener("click", async () => {
    try { setStatus(await api("POST", `/api/shop/payment-requests/${current.id}/cancel`)); } catch (e) { show("shop-msg", e.message, "bad"); }
  });
  $("new-qr").addEventListener("click", () => { $("qr-card").classList.add("hidden"); $("amount").value = ""; $("amount").focus(); });
  $("view-receipt").addEventListener("click", () => current.receipt_number && viewReceipt(current.receipt_number));
  $("copy-code").addEventListener("click", () => navigator.clipboard && navigator.clipboard.writeText(current.code));

  $("pair-btn").addEventListener("click", async () => {
    show("pair-msg", "");
    try {
      const p = await api("GET", "/api/shop/pairing");
      $("pair-img").src = p.qr_svg;
      $("pair-shop").textContent = p.shop;
      $("pair-code").textContent = p.code;
      $("pair-box").classList.remove("hidden");
      $("pair-btn").classList.add("hidden");
    } catch (e) { show("pair-msg", e.message, "bad"); }
  });

  await refresh();
  setInterval(() => refresh().catch(() => {}), 15000);
})();
