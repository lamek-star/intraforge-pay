"use strict";
document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  IFPay.show("login-msg", "");
  try {
    const data = await IFPay.api("POST", "/api/auth/login", {
      username: document.getElementById("username").value,
      password: document.getElementById("password").value,
    });
    location.href = data.home;
  } catch (err) {
    IFPay.show("login-msg", err.message, "bad");
    button.disabled = false;
  }
});
