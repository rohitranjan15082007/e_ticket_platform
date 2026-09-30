import {clear, el, field, link, post, saveToken, showStatus, withError} from "./api.js";

function safeNext() {
  const value = new URLSearchParams(location.search).get("next");
  return value && value.startsWith("/") && !value.startsWith("//") && !value.includes("\\") ? value : "/dashboard";
}
function switchLink(message, label, href) {
  const row = el("p", message, "auth-switch");
  row.append(link(label, href));
  return row;
}
function setBusy(form, submit, busy, label) {
  submit.disabled = busy;
  submit.textContent = busy ? "Please wait..." : label;
  form.setAttribute("aria-busy", String(busy));
}
function login() {
  const form = el("form", "", "card form-card");
  field(form, "Email address", "email", "email", {autocomplete: "email"}).placeholder = "you@example.com";
  field(form, "Password", "password", "password", {autocomplete: "current-password"}).placeholder = "Enter your password";
  const submit = el("button", "Sign in", "button"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", withError(async event => {
    event.preventDefault();
    setBusy(form, submit, true, "Sign in");
    try {
      const data = await post("/api/v1/auth/login", {email: form.elements.email.value, password: form.elements.password.value}, false);
      saveToken(data.token.access_token);
      location.assign(safeNext());
    } finally { setBusy(form, submit, false, "Sign in"); }
  }));
  clear().append(form, switchLink("New here?", "Create an account", "/register"));
}
function register() {
  const form = el("form", "", "card form-card");
  field(form, "Full name", "full_name", "text", {autocomplete: "name"}).placeholder = "Your name";
  field(form, "Email address", "email", "email", {autocomplete: "email"}).placeholder = "you@example.com";
  const password = field(form, "Password", "password", "password", {autocomplete: "new-password"});
  password.minLength = 12;
  password.placeholder = "At least 12 characters";
  form.append(el("span", "Use at least 12 characters.", "field-hint"));
  const submit = el("button", "Create account", "button"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", withError(async event => {
    event.preventDefault();
    setBusy(form, submit, true, "Create account");
    try {
      await post("/api/v1/auth/register", {
        full_name: form.elements.full_name.value.trim(), email: form.elements.email.value,
        password: form.elements.password.value,
      }, false);
      showStatus("Account created. Please sign in.", "success");
      form.reset();
    } finally { setBusy(form, submit, false, "Create account"); }
  }));
  clear().append(form, switchLink("Already registered?", "Sign in", "/login"));
}
export function render(page) {
  if (page === "login") return login();
  if (page === "register") return register();
}
