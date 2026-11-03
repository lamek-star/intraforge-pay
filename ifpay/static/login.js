"use strict";
if (new URLSearchParams(location.search).get("reason") === "no-access") {
  IFPay.show("login-note", "You are signed in to IntraForge, but you have no IntraForge Pay role in this shop. Ask the shop owner for Pay Cashier, Pay Supervisor or Pay Manager (IntraForge > Members).", "info");
}
document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  IFPay.show("login-msg", "");
  try {
    const data = await IFPay.api("POST", "api/auth/login", {
      username: document.getElementById("username").value,
      password: document.getElementById("password").value,
    });
    location.href = data.home;
  } catch (err) {
    IFPay.show("login-msg", err.message, "bad");
    button.disabled = false;
  }
});
