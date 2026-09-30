const TOKEN_KEY = "eticket.access_token";

export const token = () => sessionStorage.getItem(TOKEN_KEY);
export const saveToken = value => sessionStorage.setItem(TOKEN_KEY, value);
export const signOut = () => sessionStorage.removeItem(TOKEN_KEY);
export const content = document.getElementById("page-content");

export function clear(node = content) { node.replaceChildren(); return node; }
export function el(tag, value = "", className = "") {
  const node = document.createElement(tag);
  if (value !== null && value !== undefined) node.textContent = String(value);
  if (className) node.className = className;
  return node;
}
export function link(label, href, className = "") {
  const node = el("a", label, className);
  node.href = href;
  return node;
}
export function button(label, action, className = "button") {
  const node = el("button", label, className);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}
export function card(title, description = "") {
  const node = el("section", "", "card");
  node.append(el("h2", title));
  if (description) node.append(el("p", description, "muted"));
  return node;
}
export function field(form, label, name, type = "text", options = {}) {
  const wrap = el("label", "", "field");
  wrap.append(el("span", label));
  const input = document.createElement("input");
  input.name = name;
  input.type = type;
  input.required = options.required !== false;
  if (options.min !== undefined) input.min = String(options.min);
  if (options.max !== undefined) input.max = String(options.max);
  if (options.step !== undefined) input.step = String(options.step);
  if (options.autocomplete) input.autocomplete = options.autocomplete;
  if (options.value !== undefined) input.value = options.value;
  wrap.append(input);
  form.append(wrap);
  return input;
}
export function showStatus(message, kind = "info") {
  const node = document.getElementById("status");
  node.hidden = !message;
  node.className = `notice ${kind}`;
  node.textContent = message || "";
  if (message) node.scrollIntoView({block: "nearest"});
}
export function errorMessage(error) {
  return error instanceof Error ? error.message : "Something went wrong. Please retry.";
}
function detailText(body, fallback) {
  const detail = body?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(item => item.msg || "Invalid value").join("; ");
  return fallback;
}
export async function request(path, {method = "GET", body, authenticated = false, idempotent = false} = {}) {
  const headers = {Accept: "application/json"};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (authenticated) {
    if (!token()) throw new Error("Please sign in first.");
    headers.Authorization = `Bearer ${token()}`;
  }
  if (idempotent) headers["Idempotency-Key"] = crypto.randomUUID();
  let response;
  try {
    response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body)});
  } catch {
    throw new Error("Network unavailable. Please check your connection.");
  }
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && authenticated) signOut();
    throw new Error(detailText(data, `Request failed (${response.status}).`));
  }
  return data;
}
export const get = (path, authenticated = false) => request(path, {authenticated});
export const post = (path, body = {}, authenticated = true) =>
  request(path, {method: "POST", body, authenticated, idempotent: true});
export async function requireUser(admin = false) {
  if (!token()) { location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search)}`); return null; }
  try {
    const user = await get("/api/v1/auth/me", true);
    if (admin && !user.roles.includes("admin")) {
      clear().append(el("p", "Administrator access is required."));
      return null;
    }
    return user;
  } catch {
    location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
    return null;
  }
}
export function money(paise, currency = "INR") {
  if (currency !== "INR" || !Number.isSafeInteger(paise)) return "Amount unavailable";
  const negative = paise < 0 ? "-" : "";
  const abs = Math.abs(paise);
  const rupees = Math.floor(abs / 100).toLocaleString("en-IN");
  return `${negative}₹${rupees}.${String(abs % 100).padStart(2, "0")}`;
}
export function toPaise(value) {
  if (!/^(0|[1-9]\d*)(\.\d{1,2})?$/.test(value)) throw new Error("Enter rupees with at most two decimal places.");
  const [whole, decimal = ""] = value.split(".");
  const paise = BigInt(whole) * 100n + BigInt(decimal.padEnd(2, "0"));
  if (paise <= 0n || paise > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("Amount is out of range.");
  return Number(paise);
}
export function date(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Unknown date" : parsed.toLocaleString("en-IN");
}
export function empty(message) { clear().append(el("p", message, "empty-state")); }
export function withError(action) {
  return async event => {
    try { showStatus(""); await action(event); }
    catch (error) { showStatus(errorMessage(error), "error"); }
  };
}
