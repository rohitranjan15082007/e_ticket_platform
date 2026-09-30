import {button, card, clear, date, el, empty, field, get, link, money, post, requireUser, showStatus, withError} from "./api.js";

function orderCard(order) {
  const node = card(order.items.map(item => item.product_name_snapshot).join(", "), `Order ${order.id}`);
  node.append(el("p", `Total ${money(order.total_paise)} · ${order.status} · Delivery ${order.delivery_status}`, "small"));
  node.append(el("p", `Expires ${date(order.expires_at)}`, "muted small"));
  if (!["CANCELLED", "REFUNDED"].includes(order.status)) {
    node.append(link(order.status === "PENDING_PAYMENT" ? "Continue checkout" : "View checkout status",
      `/checkout?order=${encodeURIComponent(order.id)}`, "button secondary"));
  }
  return node;
}
async function dashboard(user) {
  const [orders, tickets, wallet] = await Promise.all([
    get("/api/v1/orders", true), get("/api/v1/tickets", true), get("/api/v1/wallet", true),
  ]);
  const welcome = card(`Welcome${user.full_name ? `, ${user.full_name}` : ""}`, user.email);
  const metrics = el("div", "", "grid");
  for (const [label, value, href] of [
    ["Tickets", tickets.length, "/tickets"], ["Orders", orders.length, "/dashboard"],
    ["Available wallet", wallet ? money(wallet.available_paise) : "Not provisioned", "/wallet"],
  ]) {
    const node = card(label);
    node.append(el("p", value, "metric"), link("View", href));
    metrics.append(node);
  }
  const actions = el("div", "", "actions");
  [["Wallet", "/wallet"], ["Withdrawals", "/withdrawals"], ["Referrals", "/referrals"], ["Results", "/results"]]
    .forEach(([label, href]) => actions.append(link(label, href, "button secondary")));
  const list = el("div", "", "stack");
  orders.forEach(order => list.append(orderCard(order)));
  clear().append(welcome, metrics, actions, el("h2", "Your orders"), orders.length ? list : el("p", "No orders yet. Browse the available series.", "empty-state"));
}
async function tickets() {
  const items = await get("/api/v1/tickets", true);
  if (!items.length) return empty("No delivered tickets yet. Paid orders may still be awaiting delivery.");
  const list = el("ul", "", "list");
  for (const ticket of items) {
    const row = el("li", "", "list-item");
    row.append(el("h2", `Ticket #${ticket.serial_number}`));
    row.append(el("p", `Series ${ticket.series_id} · ${ticket.status}`, "muted"));
    if (ticket.is_winner) row.append(el("p", `Winner · Prize ${money(ticket.prize_paise)}`, "pill"));
    list.append(row);
  }
  clear().append(list);
}
async function referrals() {
  const [profile, claim] = await Promise.all([
    get("/api/v1/referrals/profile", true), get("/api/v1/referrals/claim", true),
  ]);
  const root = clear();
  const own = card("Your referral code", "Share this code with someone before their first qualifying settlement.");
  if (profile) own.append(el("p", profile.code, "metric"));
  else own.append(button("Create my code", withError(async () => {
    await post("/api/v1/referrals/profile");
    showStatus("Referral code created.", "success");
    await referrals();
  })));
  root.append(own);
  const inbound = card("Use a referral code");
  if (claim) inbound.append(el("p", `Code ${claim.referral_code_snapshot} · Status ${claim.status}`));
  else {
    const form = el("form");
    field(form, "Referral code", "referral_code");
    const submit = el("button", "Claim code", "button"); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", withError(async event => {
      event.preventDefault();
      submit.disabled = true;
      try {
        await post("/api/v1/referrals/claim", {referral_code: form.elements.referral_code.value.trim()});
        showStatus("Referral recorded; rewards depend on a qualifying settlement and review.", "success");
        await referrals();
      } finally { submit.disabled = false; }
    }));
    inbound.append(form);
  }
  root.append(inbound);
}
export async function render(page) {
  const user = await requireUser();
  if (!user) return;
  if (page === "dashboard") return dashboard(user);
  if (page === "tickets") return tickets();
  if (page === "referrals") return referrals();
}
