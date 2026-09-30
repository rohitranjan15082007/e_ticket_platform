import {button, card, clear, date, el, field, get, link, money, post, requireUser, showStatus, toPaise, withError} from "./api.js";
import {navigation} from "./account_v2.js";

const params = new URLSearchParams(location.search);
const orderId = params.get("order");
const attemptId = params.get("attempt");
const matchId = params.get("match");
function readable(value) {
  return String(value).replaceAll("_", " ").toLowerCase().replace(/\b\w/g, character => character.toUpperCase());
}
function step(number) {
  const list = el("ol", "", "step-list");
  ["Choose", "Review", "Pay"].forEach((label, index) => {
    list.append(el("li", `${index + 1}. ${label}`, index + 1 === number ? "current" : ""));
  });
  return list;
}
function navigate(key, id, order = orderId) {
  location.assign(`/checkout?order=${encodeURIComponent(order)}&${key}=${encodeURIComponent(id)}`);
}
function facts(node, object) {
  for (const [key, value] of Object.entries(object || {})) {
    if (value === null || value === undefined || value === "") continue;
    if (typeof value === "object") { facts(node, value); continue; }
    node.append(el("p", `${key.replaceAll("_", " ")}: ${String(value)}`, "small"));
  }
}
async function selectProduct() {
  const kind = params.get("type");
  const id = params.get("id");
  if (!["SERIES", "PACKAGE"].includes(kind) || !id) {
    clear().append(step(1), el("p", "Choose a product to begin."), link("Browse series", "/series", "button"));
    return;
  }
  const product = await get(`/api/v1/catalog/${kind === "SERIES" ? "series" : "packages"}/${encodeURIComponent(id)}`);
  const form = el("form", "", "card form-card");
  form.classList.add("checkout-form");
  form.append(el("h2", product.name), el("p", product.description || "Ticket product", "muted"));
  form.append(el("p", `Unit price: ${money(product.price_paise)}`, "metric"));
  field(form, "Quantity", "quantity", "number", {min: 1, max: 10000, value: 1});
  field(form, "Coupon code (optional)", "coupon_code", "text", {required: false});
  const submit = el("button", "Reserve and review", "button"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", withError(async event => {
    event.preventDefault();
    const quantity = Number(form.elements.quantity.value);
    if (!Number.isSafeInteger(quantity) || quantity < 1 || quantity > 10000) throw new Error("Enter a valid quantity.");
    submit.disabled = true;
    try {
      const order = await post("/api/v1/orders", {
        product_type: kind, product_id: id, quantity,
        coupon_code: form.elements.coupon_code.value.trim() || null,
      });
      navigate("step", "review", order.id);
    } finally { submit.disabled = false; }
  }));
  clear().append(step(1), form, el("p", "Reservation does not confirm payment or ticket delivery.", "checkout-hint"));
}
function orderSummary(order) {
  const node = card("Server-priced order", `Order ${order.id} · Reservation deadline ${date(order.expires_at)}`);
  node.classList.add("checkout-summary");
  for (const item of order.items) node.append(el("p", `${item.product_name_snapshot} × ${item.quantity}: ${money(item.line_total_paise)}`));
  node.append(el("p", `Subtotal: ${money(order.subtotal_paise)}`));
  if (order.discount_paise) node.append(el("p", `Discount: ${money(order.discount_paise)}`));
  node.append(el("p", `Total: ${money(order.total_paise)}`, "metric"));
  node.append(el("p", `Payment: ${readable(order.status)} · Delivery: ${readable(order.delivery_status)}`, "pill"));
  return node;
}
async function review() {
  const order = await get(`/api/v1/orders/${encodeURIComponent(orderId)}`, true);
  const root = clear();
  root.append(step(2));
  const layout = el("div", "", "checkout-layout");
  const main = el("div", "", "checkout-main");
  layout.append(orderSummary(order), main);
  root.append(layout);
  if (order.status !== "PENDING_PAYMENT") {
    const state = await get(`/api/v1/orders/${encodeURIComponent(order.id)}/checkout-state`, true);
    main.append(el("p", "The server has already chosen or finished this payment path. The order status above is authoritative.", "notice"));
    if (state.payment_attempt_id) main.append(link("View payment attempt", `/checkout?order=${encodeURIComponent(order.id)}&attempt=${encodeURIComponent(state.payment_attempt_id)}`, "button secondary"));
    else if (state.p2p_match_id) main.append(link("View P2P match", `/checkout?order=${encodeURIComponent(order.id)}&match=${encodeURIComponent(state.p2p_match_id)}`, "button secondary"));
    else if (order.status === "WAITING_FOR_MATCH") main.append(el("p", "Still waiting for an exact P2P match. Refresh this page later.", "muted"));
    main.append(link("Order detail", `/orders/${encodeURIComponent(order.id)}`, "button"));
    return;
  }
  let available;
  try { available = await get("/api/v1/payment-methods", true); }
  catch {
    main.append(el("p", "Payment methods could not be loaded. Refresh before starting a payment path.", "notice error"));
    return;
  }
  const methods = card("Choose a payment method", "Only configured paths are shown. The server rechecks your order and method when you continue.");
  methods.classList.add("payment-methods");
  const actions = el("div", "", "method-list");
  for (const [label, method, description] of [
    ["Manual UPI", "MANUAL_UPI", "Pay using the displayed approved destination, then submit evidence for review."],
    ["P2P match", "P2P", "Request an exact-amount match. You may be queued until a qualifying receiver is available."],
  ]) {
    if ((method === "MANUAL_UPI" && !available.manual_upi) || (method === "P2P" && !available.p2p_match)) continue;
    const control = button(label, withError(async () => {
      control.disabled = true;
      try {
        if (method === "P2P") {
          const result = await post(`/api/v1/orders/${encodeURIComponent(order.id)}/p2p/match`);
          if (result.match) navigate("match", result.match.id);
          else { showStatus(result.message || "Waiting for a match.", "info"); await review(); }
        } else {
          const result = await post(`/api/v1/orders/${encodeURIComponent(order.id)}/payments`, {method});
          navigate("attempt", result.payment.id);
        }
      } finally { control.disabled = false; }
    }), "button secondary");
    const option = el("section", "", "method-option");
    option.append(el("h3", label), el("p", description, "muted small"), control);
    actions.append(option);
  }
  methods.append(actions);
  if (available.telegram_stars) {
    const telegramForm = el("form", "", "method-option telegram-option");
    telegramForm.append(el("h2", "Telegram Stars"));
    telegramForm.append(el("p", "Invoice preparation is not payment. The configured bot must dispatch and confirm a completed invoice.", "muted small"));
    field(telegramForm, "Your Telegram numeric user ID", "telegram_user_id", "text");
    const telegramButton = el("button", "Prepare Stars invoice", "button secondary");
    telegramButton.type = "submit"; telegramForm.append(telegramButton);
    telegramForm.addEventListener("submit", withError(async event => {
      event.preventDefault();
      const value = telegramForm.elements.telegram_user_id.value.trim();
      if (!/^[1-9]\d*$/.test(value) || !Number.isSafeInteger(Number(value))) throw new Error("Enter a valid numeric Telegram user ID.");
      telegramButton.disabled = true;
      try {
        const result = await post(`/api/v1/orders/${encodeURIComponent(order.id)}/payments`, {method: "TELEGRAM_STARS", telegram_user_id: Number(value)});
        navigate("attempt", result.payment.id);
      } finally { telegramButton.disabled = false; }
    }));
    methods.append(telegramForm);
  }
  methods.append(el("p", "No method is considered paid until independent server confirmation. White-label initiation is unavailable.", "checkout-hint"));
  main.append(methods);
}
function manualProof(attempt) {
  const form = el("form", "", "card form-card");
  form.classList.add("evidence-form");
  form.append(el("h2", "Submit payment evidence"), el("p", "A UTR and evidence reference start review only. They do not confirm payment.", "muted"));
  field(form, "UTR / transaction reference", "utr");
  field(form, "Amount actually paid (rupees)", "amount", "text");
  field(form, "Evidence reference (existing secure URL or reference)", "proof_reference");
  const submit = el("button", "Submit for review", "button"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", withError(async event => {
    event.preventDefault();
    const submitted_amount_paise = toPaise(form.elements.amount.value.trim());
    submit.disabled = true;
    try {
      const response = await post(`/api/v1/payments/${encodeURIComponent(attempt.id)}/manual-proof`, {
        utr: form.elements.utr.value.trim(),
        proof_reference: form.elements.proof_reference.value.trim(),
        submitted_amount_paise,
        submitted_currency: "INR",
      });
      showStatus(`Evidence received. Payment is ${readable(response.payment.status)}; administrator review is required.`, "success");
      await payment();
    } finally { submit.disabled = false; }
  }));
  return form;
}
async function payment() {
  const attempt = await get(`/api/v1/payments/${encodeURIComponent(attemptId)}`, true);
  const root = clear();
  root.append(step(3));
  const info = card(`${readable(attempt.method)} payment`, `Status: ${readable(attempt.status)} · Expires ${date(attempt.expires_at)}`);
  info.classList.add("payment-instructions");
  info.append(el("p", `Order amount: ${money(attempt.order_amount_paise)}`, "metric"));
  if (attempt.provider_currency !== "INR") info.append(el("p", `Provider amount: ${attempt.provider_amount} ${attempt.provider_currency}`));
  if (attempt.method === "MANUAL_UPI") {
    const destination = attempt.method_data_snapshot?.manual_destination;
    if (destination) {
      info.append(el("p", `Payee: ${destination.display_label || "Approved destination"}`));
      info.append(el("p", `UPI ID: ${destination.upi_id || "Unavailable"}`));
      if (destination.qr_reference) info.append(el("p", `QR reference: ${destination.qr_reference}`));
      facts(info, destination.instructions);
    }
  } else if (attempt.method === "TELEGRAM_STARS") {
    const username = attempt.method_data_snapshot?.telegram_bot_username;
    if (username) info.append(el("p", `Configured bot: ${username}`));
  }
  if (attempt.method === "TELEGRAM_STARS") info.append(el("p", "Invoice preparation is not a payment. Complete the invoice in the configured bot when available; this website does not dispatch it.", "notice"));
  root.append(info);
  if (attempt.method === "MANUAL_UPI" && attempt.status === "AWAITING_PAYMENT") root.append(manualProof(attempt));
  root.append(link("View order status", `/orders/${encodeURIComponent(attempt.order_id)}`, "button secondary"));
}
function matchProof(match) {
  const form = el("form", "", "card form-card");
  form.classList.add("evidence-form");
  form.append(el("h2", "Submit P2P payment claim"), el("p", "A claim does not settle a match. Provider evidence and receiver/admin verification are required.", "muted"));
  field(form, "Payment reference", "claimed_reference");
  field(form, "Amount actually paid (rupees)", "amount", "text");
  field(form, "Provider evidence reference (optional)", "provider_evidence_reference", "text", {required: false});
  const submit = el("button", "Submit claim", "button"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", withError(async event => {
    event.preventDefault();
    const observed_amount_paise = toPaise(form.elements.amount.value.trim());
    const provider_namespace = match.destination_snapshot?.provider_namespace;
    if (!provider_namespace) throw new Error("Verified destination details are unavailable. Do not submit a payment claim.");
    submit.disabled = true;
    try {
      const response = await post(`/api/v1/matches/${encodeURIComponent(match.id)}/payment-submissions`, {
        provider_namespace,
        claimed_reference: form.elements.claimed_reference.value.trim(),
        provider_evidence_reference: form.elements.provider_evidence_reference.value.trim() || null,
        observed_amount_paise,
        observed_currency: "INR",
        declared_paid_at: new Date().toISOString(),
      });
      showStatus(`Claim recorded. Verification: ${readable(response.submission.verification_status)}. This is not settlement.`, "success");
      await matched();
    } finally { submit.disabled = false; }
  }));
  return form;
}
async function matched() {
  const match = await get(`/api/v1/matches/${encodeURIComponent(matchId)}`, true);
  const info = card("P2P match", `Status: ${readable(match.status)} · Payment deadline ${date(match.payment_deadline_at)}`);
  info.classList.add("payment-instructions");
  info.append(el("p", `Exact amount: ${money(match.amount_paise)}`, "metric"));
  if (match.destination_exposed) facts(info, match.destination_snapshot);
  const root = clear();
  root.append(step(3), info);
  if (match.destination_exposed && match.status === "WAITING_FOR_PAYMENT") root.append(matchProof(match));
  root.append(link("View order status", `/orders/${encodeURIComponent(match.order_id)}`, "button secondary"));
}
export async function render() {
  if (!await requireUser()) return;
  navigation("checkout");
  if (attemptId) return payment();
  if (matchId) return matched();
  if (orderId) return review();
  return selectProduct();
}
