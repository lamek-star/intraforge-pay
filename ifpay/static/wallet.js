"use strict";
(async () => {
  const { api, el, show, fmtTime, openReceipt } = IFPay;
  if (!(await IFPay.requireUser("customer", "wallet.read"))) return;
  const $ = (id) => document.getElementById(id);
  let stream = null;
  let scanning = false;
  let pending = null; // {code, info}

  async function refresh() {
    const w = await api("GET", "api/wallet");
    $("customer-name").textContent = w.customer;
    $("balance").replaceChildren(w.balance, el("small", {}, " IF Credits"));
    $("history").replaceChildren(...w.history.map((h) => el("li", {},
      el("div", { class: "grow" }, el("div", {}, h.description), el("div", { class: "small muted" }, fmtTime(h.at))),
      el("span", { class: `amount ${h.amount.startsWith("-") ? "neg" : "pos"}` }, h.amount),
      h.receipt_number ? el("button", { class: "secondary", onclick: () => openReceipt(h.receipt_number) }, "Receipt") : null)));
    $("history-empty").classList.toggle("hidden", w.history.length > 0);
  }

  // --- scanning ------------------------------------------------------------------------------------------------------
  function stopCamera() {
    scanning = false;
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    $("video").classList.add("hidden");
  }

  function decodeImageData(imageData) {
    const hit = window.jsQR && jsQR(imageData.data, imageData.width, imageData.height, { inversionAttempts: "attemptBoth" });
    return hit ? hit.data : null;
  }

  async function startCamera() {
    show("scan-msg", "");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      show("scan-msg", "The live camera needs a secure (https or localhost) page. Take a photo of the QR instead.", "info");
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
    } catch (e) {
      show("scan-msg", "Camera not available. Take a photo of the QR or paste the code.", "info");
      return;
    }
    const video = $("video");
    video.srcObject = stream;
    video.classList.remove("hidden");
    await video.play();
    scanning = true;
    const detector = "BarcodeDetector" in window ? new BarcodeDetector({ formats: ["qr_code"] }) : null;
    const canvas = $("canvas");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    const tick = async () => {
      if (!scanning) return;
      let value = null;
      try {
        if (detector) {
          const codes = await detector.detect(video);
          value = codes.length ? codes[0].rawValue : null;
        } else if (video.videoWidth) {
          canvas.width = video.videoWidth; canvas.height = video.videoHeight;
          ctx.drawImage(video, 0, 0);
          value = decodeImageData(ctx.getImageData(0, 0, canvas.width, canvas.height));
        }
      } catch (_) { /* next frame */ }
      if (value) { stopCamera(); resolveCode(value); return; }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  async function decodePhoto(file) {
    const bitmap = await createImageBitmap(file);
    const canvas = $("canvas");
    const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
    canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    return decodeImageData(ctx.getImageData(0, 0, canvas.width, canvas.height));
  }

  // --- confirm and pay -----------------------------------------------------------------------------------------------
  async function resolveCode(code) {
    show("scan-msg", "");
    try {
      const info = await api("POST", "api/wallet/checkout/resolve", { code: code.trim() });
      pending = { code: code.trim(), info };
      $("scan-card").classList.add("hidden");
      $("merchant-name").textContent = info.merchant.name;
      const v = $("merchant-verified");
      v.textContent = info.merchant.verified ? "✓ Verified by IntraForge" : "Not verified";
      v.className = `badge ${info.merchant.verified ? "ok" : "bad"}`;
      $("confirm-amount").textContent = `${info.amount} IF Credits`;
      // What the payment is for, when another application in the shop asked for it (e.g. a Spare Parts invoice).
      const forLine = $("confirm-for");
      forLine.textContent = info.reference ? `For: ${info.reference}${info.requested_by_app ? ` (${info.requested_by_app})` : ""}` : "";
      forLine.classList.toggle("hidden", !info.reference);
      $("confirm-expiry").textContent = info.status === "pending" ? `This request expires at ${new Date(info.expires_at).toLocaleTimeString()}.` : "";
      $("pay-btn").textContent = `Confirm and pay ${info.amount}`;
      const payable = info.status === "pending" && info.merchant.verified;
      $("pay-btn").disabled = !payable;
      show("confirm-msg", info.status === "pending" ? (info.merchant.verified ? "" : "This shop is not verified. Do not pay.")
        : `This payment request is ${info.status.replace("_", " ")}.`, payable ? "info" : "bad");
      $("confirm-card").classList.remove("hidden");
      $("pay-btn").focus();
    } catch (e) {
      show("scan-msg", e.message, "bad");
      $("scan-card").classList.remove("hidden");
    }
  }

  $("pay-btn").addEventListener("click", async () => {
    if (!pending) return;
    $("pay-btn").disabled = true;
    try {
      const r = await api("POST", "api/wallet/checkout/pay",
        { code: pending.code, expected_amount_minor: pending.info.amount_minor, confirm: true });
      show("confirm-msg", r.status === "paid" ? `Paid. New balance: ${r.balance} IF Credits.` : "Already paid — showing your receipt.", "ok");
      pending = null;
      await refresh();
      await openReceipt(r.receipt.receipt_number);
      $("confirm-card").classList.add("hidden");
    } catch (e) {
      show("confirm-msg", e.message, "bad");
    }
  });

  $("confirm-cancel").addEventListener("click", () => { pending = null; $("confirm-card").classList.add("hidden"); });
  $("scan-btn").addEventListener("click", () => {
    $("confirm-card").classList.add("hidden");
    $("scan-card").classList.remove("hidden");
    $("code-input").value = "";
    show("scan-msg", "");
  });
  $("scan-close").addEventListener("click", () => { stopCamera(); $("scan-card").classList.add("hidden"); });
  $("camera-btn").addEventListener("click", startCamera);
  $("photo-btn").addEventListener("click", () => $("photo-input").click());
  $("photo-input").addEventListener("change", async () => {
    const file = $("photo-input").files[0];
    if (!file) return;
    try {
      const value = await decodePhoto(file);
      if (value) resolveCode(value); else show("scan-msg", "No QR code found in that photo. Try again closer, or paste the code.", "bad");
    } catch (_) { show("scan-msg", "Could not read that image.", "bad"); }
  });
  $("code-btn").addEventListener("click", () => $("code-input").value.trim() && resolveCode($("code-input").value));

  await refresh();
})();
