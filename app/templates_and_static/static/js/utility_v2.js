import {button, clear, date, el, field, get, link, money, post, requireUser, showStatus, withError} from "./api.js";
import {navigation} from "./account_v2.js";

const path = location.pathname, id = path.split("/").filter(Boolean).at(-1);
const readable = value => String(value ?? "Unavailable").replaceAll("_", " ").toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
function heading(title, description) {
  document.title = `${title} · E-Ticket`;
  document.getElementById("utility-heading").replaceChildren(
    el("p", "YOUR ACCOUNT", "eyebrow"), el("h1", title), el("p", description),
  );
}
function panel(title, rows) {
  const node = el("section", "", "detail-card");
  node.append(el("h2", title));
  const list = el("dl");
  for (const [label, value] of rows) {
    if (value === null || value === undefined || value === "") continue;
    list.append(el("dt", label), el("dd", String(value)));
  }
  node.append(list); return node;
}
function notice(text) { return el("p", text, "notice"); }
async function referrals() {
  heading("Rewards & referrals", "Your code and claim state; a pending reward is not wallet credit.");
  const [profile, claim] = await Promise.all([
    get("/api/v1/referrals/profile", true), get("/api/v1/referrals/claim", true),
  ]);
  const root = clear(), grid = el("div", "", "detail-grid");
  const own = el("section", "", "detail-card");
  own.append(el("h2", "Your referral code"), el("p", "Share this before a first qualifying settlement.", "muted"));
  if (profile) own.append(el("p", profile.code, "metric"));
  else own.append(button("Create my code", withError(async () => {
    await post("/api/v1/referrals/profile");
    showStatus("Referral code created.", "success"); await referrals();
  })));
  const inbound = el("section", "", "detail-card");
  inbound.append(el("h2", "Use a referral code"));
  if (claim) inbound.append(el("p", `Code ${claim.referral_code_snapshot} · ${readable(claim.status)}`));
  else {
    const form = el("form", "", "form-card");
    field(form, "Referral code", "referral_code");
    const submit = el("button", "Record code", "button"); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", withError(async event => {
      event.preventDefault(); submit.disabled = true;
      try {
        await post("/api/v1/referrals/claim", {referral_code: form.elements.referral_code.value.trim()});
        showStatus("Code recorded; any reward still depends on settlement and review.", "success");
        await referrals();
      } finally { submit.disabled = false; }
    }));
    inbound.append(form);
  }
  grid.append(own, inbound);
  root.append(notice("Referral, cashback and affiliate records are not paid rewards until server-approved posting."), grid);
}
async function ticketDetail() {
  heading("Ticket detail", "Ticket identity and result are server-recorded; the card is decorative.");
  const tickets = await get("/api/v1/tickets", true);
  const ticket = tickets.find(item => item.id === id);
  const root = clear();
  root.append(link("Back to tickets", "/tickets", "button secondary"));
  if (!ticket) { root.append(el("p", "Ticket not found in your delivered tickets.", "empty-state")); return; }
  root.append(panel(`Ticket #${ticket.serial_number}`, [
    ["Ticket ID", ticket.id], ["Series ID", ticket.series_id], ["Status", readable(ticket.status)],
    ["Winner recorded", ticket.is_winner ? "Yes" : "No"],
    ["Prize", ticket.is_winner ? money(ticket.prize_paise) : "No prize recorded"],
  ]), link("Published series result", `/results/${encodeURIComponent(ticket.series_id)}`, "button secondary"),
  notice("A scratch animation cannot reveal or alter the result. Only published server records determine outcomes."));
}
async function withdrawalDetail() {
  heading("Withdrawal timeline", "Request history and amount snapshots from the server.");
  const [item, destinations] = await Promise.all([
    get(`/api/v1/withdrawals/${encodeURIComponent(id)}`, true),
    get("/api/v1/withdrawals/destinations", true),
  ]);
  const destination = destinations.find(value => value.id === item.payment_destination_id);
  clear().append(link("Back to withdrawals", "/withdrawals", "button secondary"),
    panel("Request", [
      ["Request ID", item.id], ["Status", readable(item.status)],
      ["Amount", money(item.amount_paise, item.currency)],
      ["Verified destination", destination?.display_label || "Destination label unavailable"],
      ["Eligible balance at request", money(item.eligible_balance_snapshot_paise, item.currency)],
      ["Maximum at request", money(item.max_amount_snapshot_paise, item.currency)],
      ["Rule version", item.rule_version],
      ["Matched", item.matched_at ? date(item.matched_at) : null],
      ["Completed", item.completed_at ? date(item.completed_at) : null],
      ["Cancelled", item.cancelled_at ? date(item.cancelled_at) : null],
    ]), notice("A hold, match or buyer claim is not a completed payout. The status above comes from the server."));
}
async function account() {
  heading("Account", "Identity and destination status available to this signed-in user.");
  const [user, destinations] = await Promise.all([
    get("/api/v1/profile", true), get("/api/v1/withdrawals/destinations", true),
  ]);
  const root = clear();
  root.append(panel("Profile", [
    ["User ID", user.id], ["Email", user.email],
    ["Name", user.full_name || "Not provided"], ["Status", user.is_active ? "Active" : "Inactive"],
    ["Verified", user.is_verified ? "Yes" : "No"], ["Created", date(user.created_at)],
  ]));
  const dest = el("section", "", "detail-card");
  dest.append(el("h2", "Operations-verified destinations"));
  if (!destinations.length) dest.append(el("p", "No verified destination is available.", "empty-state"));
  for (const item of destinations) dest.append(el("p",
    `${item.display_label} · ${item.provider_namespace} · Verified ${date(item.verified_at)}`));
  root.append(dest, notice("This page cannot enroll or self-verify a payout destination. Operations controls verification."));
}
async function notifications() {
  heading("Notifications", "Delivered P2P and draw events recorded for your account.");
  const items = await get("/api/v1/notifications?limit=50", true);
  const root = clear();
  root.append(notice("Only delivered in-app event metadata is shown. A notification is not payment or payout confirmation."));
  if (!items.length) {
    root.append(el("p", "No delivered notifications yet.", "empty-state"));
    return;
  }
  const list = el("div", "", "order-list");
  for (const item of items) {
    const card = el("article", "", "order-card");
    card.append(el("h2", readable(item.event_type)),
      el("p", `${item.source} · ${date(item.occurred_at)}`, "muted small"));
    list.append(card);
  }
  root.append(list);
}
export async function render() {
  const user = await requireUser();
  if (!user) return;
  const page = path === "/referrals" ? "referrals" : path === "/account" ? "account" :
    path === "/notifications" ? "notifications" :
    path.startsWith("/tickets/") ? "ticket-detail" : "withdrawal-detail";
  navigation(page);
  if (page === "referrals") return referrals();
  if (page === "account") return account();
  if (page === "notifications") return notifications();
  if (page === "ticket-detail") return ticketDetail();
  return withdrawalDetail();
}
