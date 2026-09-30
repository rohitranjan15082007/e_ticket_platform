import {clear, date, el, get, link, money, requireUser, signOut} from "./api.js";

const finalStatuses = new Set(["FULFILLED", "CANCELLED", "REFUNDED"]);
const reviewStatuses = new Set(["PAYMENT_REVIEW", "REFUND_PENDING"]);
const goodStatuses = new Set(["PAID", "FULFILLED", "DELIVERED"]);

function label(value) {
  return String(value).replaceAll("_", " ").toLowerCase().replace(/\b\w/g, character => character.toUpperCase());
}
function chip(value) {
  const node = el("span", label(value), "status-chip");
  node.dataset.tone = goodStatuses.has(value) ? "good" : reviewStatuses.has(value) ? "review" : finalStatuses.has(value) ? "muted" : "default";
  return node;
}
function shortId(value) { return String(value).slice(0, 8); }
function orderItemIndex(orders) {
  const index = new Map();
  for (const order of orders) for (const item of order.items) index.set(item.id, {order, item});
  return index;
}
export function navigation(page) {
  const sidebar = document.getElementById("account-sidebar");
  const mobile = document.getElementById("account-mobile-nav");
  sidebar.append(el("p", "Your workspace", "sidebar-title"));
  const menu = el("nav");
  menu.setAttribute("aria-label", "Account pages");
  const links = [
    ["Dashboard", "/dashboard", "dashboard"], ["Tickets", "/tickets", "tickets"],
    ["Orders", "/orders", "orders"], ["Payments", "/payments", "payments"],
    ["Wallet", "/wallet", "wallet"],
    ["Withdrawals", "/withdrawals", "withdrawals"], ["Rewards", "/referrals", "referrals"],
    ["Account", "/account", "account"], ["Notifications", "/notifications", "notifications"],
  ];
  for (const [text, href, key] of links) {
    const item = link(text, href);
    if (page === key || (page === "order-detail" && key === "orders") ||
        (page === "ticket-detail" && key === "tickets") ||
        (page === "withdrawal-detail" && key === "withdrawals")) item.setAttribute("aria-current", "page");
    menu.append(item);
  }
  sidebar.append(menu, el("div", "", "sidebar-divider"), link("Public results", "/results"));

  for (const [text, href, key] of links.filter(([, , key]) => ["dashboard", "tickets", "orders", "wallet"].includes(key))) {
    const item = link(text === "Dashboard" ? "Home" : text, href);
    if (page === key || (page === "order-detail" && key === "orders") || (page === "ticket-detail" && key === "tickets")) item.setAttribute("aria-current", "page");
    mobile.append(item);
  }
  const more = document.createElement("details");
  more.append(el("summary", "More"));
  const moreLinks = el("div");
  moreLinks.append(link("Payments", "/payments"), link("Withdrawals", "/withdrawals"), link("Rewards", "/referrals"),
    link("Account", "/account"), link("Notifications", "/notifications"), link("Results", "/results"));
  const out = el("button", "Sign out");
  out.type = "button";
  out.addEventListener("click", () => { signOut(); location.assign("/"); });
  moreLinks.append(out);
  more.append(moreLinks);
  mobile.append(more);
}

function metricCard(title, value, href) {
  const node = el("section", "", "metric-card");
  node.append(el("p", title, "metric-label"), el("p", value, "metric-value"), link("View details", href));
  return node;
}
function sectionHead(title, href, action) {
  const node = el("div", "", "section-head");
  node.append(el("h2", title), link(action, href));
  return node;
}
function orderCard(order) {
  const node = el("article", "", "order-card");
  const top = el("div", "", "order-top");
  const title = el("div");
  title.append(el("h3", order.items.map(item => item.product_name_snapshot).join(", ") || "Ticket order"));
  title.append(el("p", `Order ${order.id}`, "order-ref"));
  top.append(title, chip(order.status));
  const facts = el("div", "", "order-facts");
  const total = el("span"); total.append("Total ", el("strong", money(order.total_paise, order.currency)));
  const delivery = el("span"); delivery.append("Delivery ", el("strong", label(order.delivery_status)));
  facts.append(total, delivery, el("span", `Reservation deadline ${date(order.expires_at)}`));
  const actions = el("div", "", "order-actions");
  actions.append(link("View details", `/orders/${encodeURIComponent(order.id)}`));
  if (order.status === "PENDING_PAYMENT") actions.append(link("Continue checkout", `/checkout?order=${encodeURIComponent(order.id)}`, "button secondary"));
  node.append(top, facts, actions);
  return node;
}
function ticketCard(ticket, source) {
  const node = el("article", "", "ticket-card");
  const face = el("div", "", "ticket-face");
  face.append(el("span", `E-Ticket · ${label(ticket.status)}`, "ticket-kicker"), el("h3", `Ticket #${ticket.serial_number}`), el("p", `Series ${shortId(ticket.series_id)}`, "ticket-serial"));
  const data = el("div", "", "ticket-data");
  data.append(chip(ticket.status));
  if (source) {
    data.append(el("p", `${source.item.product_type === "PACKAGE" ? "From package" : "Series"}: ${source.item.product_name_snapshot}`));
    data.append(link(`Order ${shortId(source.order.id)}`, `/orders/${encodeURIComponent(source.order.id)}`));
  } else data.append(el("p", `Series ID: ${ticket.series_id}`));
  if (ticket.is_winner) data.append(el("p", `Winner recorded · Prize ${money(ticket.prize_paise)}`));
  data.append(link("Ticket details", `/tickets/${encodeURIComponent(ticket.id)}`));
  data.append(link("Check public results", `/results/${encodeURIComponent(ticket.series_id)}`));
  node.append(face, data);
  return node;
}

async function dashboard(user) {
  const [orders, tickets, wallet] = await Promise.all([
    get("/api/v1/orders", true), get("/api/v1/tickets", true), get("/api/v1/wallet", true),
  ]);
  const content = clear();
  const welcome = el("div", "", "account-welcome");
  welcome.append(el("h2", `Welcome${user.full_name ? `, ${user.full_name}` : ""}`));
  welcome.append(el("span", user.email, "welcome-email"));
  content.append(welcome);
  const metrics = el("div", "", "metric-grid");
  metrics.append(
    metricCard("My tickets", tickets.length, "/tickets"),
    metricCard("Active orders", orders.filter(order => !finalStatuses.has(order.status)).length, "/orders"),
    metricCard("Available wallet", wallet ? money(wallet.available_paise, wallet.currency) : "Not provisioned", "/wallet"),
    metricCard("Held wallet", wallet ? money(wallet.locked_paise, wallet.currency) : "Not provisioned", "/wallet"),
  );
  content.append(metrics);

  const pending = orders.find(order => order.status === "PENDING_PAYMENT");
  const active = orders.find(order => !finalStatuses.has(order.status));
  const next = el("section", "", "next-step");
  const copy = el("div");
  copy.append(el("p", "Your next step", "eyebrow"));
  copy.append(el("h2", pending ? "An order is ready for checkout review" : active ? "Follow your active order" : "Explore available ticket series"));
  copy.append(el("p", pending ? "Review the server-priced amount and available payment paths." : active ? "Payment and delivery statuses update only after server confirmation." : "Browse current series before choosing a ticket."));
  next.append(copy, link(pending ? "Continue checkout" : active ? "View order" : "Browse series",
    pending ? `/checkout?order=${encodeURIComponent(pending.id)}` : active ? `/orders/${encodeURIComponent(active.id)}` : "/series", "button"));
  content.append(next, sectionHead("Recent orders", "/orders", "View all orders"));
  if (orders.length) {
    const list = el("div", "", "order-list");
    for (const order of orders.slice(0, 3)) list.append(orderCard(order));
    content.append(list);
  } else content.append(el("p", "No orders yet. Browse available series to get started.", "empty-state"));
  content.append(sectionHead("Recent tickets", "/tickets", "View all tickets"));
  if (tickets.length) {
    const list = el("div", "", "ticket-list");
    const index = orderItemIndex(orders);
    for (const ticket of tickets.slice(0, 2)) list.append(ticketCard(ticket, index.get(ticket.order_item_id)));
    content.append(list);
  } else content.append(el("p", "No delivered tickets yet. A paid order may still be awaiting delivery.", "empty-state"));
}

async function tickets() {
  const [tickets, orders] = await Promise.all([get("/api/v1/tickets", true), get("/api/v1/orders", true)]);
  const content = clear();
  if (!tickets.length) {
    content.append(el("p", "No delivered tickets yet. A paid order may still be awaiting delivery.", "empty-state"), link("Browse series", "/series", "button"));
    return;
  }
  const index = orderItemIndex(orders);
  const list = el("div", "", "ticket-list");
  for (const ticket of tickets) list.append(ticketCard(ticket, index.get(ticket.order_item_id)));
  content.append(list, el("p", "Ticket styling is decorative. A scratch gesture cannot reveal or change a result.", "ticket-caution"));
}

async function orders() {
  const orders = await get("/api/v1/orders", true);
  const content = clear();
  if (!orders.length) {
    content.append(el("p", "No orders yet. Browse current series or packages to start.", "empty-state"), link("Browse series", "/series", "button"));
    return;
  }
  const filters = el("div", "", "filter-bar");
  const searchWrap = el("label", "", "field");
  searchWrap.append(el("span", "Search orders"));
  const search = document.createElement("input");
  search.type = "search"; search.placeholder = "Product or order ID";
  searchWrap.append(search);
  const statusWrap = el("label", "", "field");
  statusWrap.append(el("span", "Status"));
  const status = document.createElement("select");
  const all = document.createElement("option"); all.value = ""; all.textContent = "All statuses"; status.append(all);
  for (const value of [...new Set(orders.map(order => order.status))].sort()) {
    const option = document.createElement("option"); option.value = value; option.textContent = label(value); status.append(option);
  }
  statusWrap.append(status);
  filters.append(searchWrap, statusWrap);
  const count = el("p", "", "filter-count");
  const list = el("div", "", "order-list");
  function update() {
    const query = search.value.trim().toLowerCase();
    const visible = orders.filter(order => (!status.value || order.status === status.value) &&
      (!query || order.id.toLowerCase().includes(query) || order.items.some(item => item.product_name_snapshot.toLowerCase().includes(query))));
    count.textContent = `Showing ${visible.length} of ${orders.length} orders`;
    list.replaceChildren();
    if (!visible.length) list.append(el("p", "No orders match these filters.", "empty-state"));
    else for (const order of visible) list.append(orderCard(order));
  }
  search.addEventListener("input", update);
  status.addEventListener("change", update);
  content.append(filters, count, list);
  update();
}

async function payments() {
  const orders = await get("/api/v1/orders", true);
  const content = clear();
  const note = el("section", "", "commerce-note");
  note.append(el("h2", "How payment status works"));
  note.append(el("p", "These are order-level states from the server. A UTR, screenshot, receiver claim or prepared invoice is not proof of settlement."));
  content.append(note);
  if (!orders.length) {
    content.append(el("p", "No order payment records yet. Browse current series to begin.", "empty-state"), link("Browse series", "/series", "button"));
    return;
  }
  const metrics = el("div", "", "metric-grid payment-metrics");
  metrics.append(
    metricCard("Awaiting action", orders.filter(order => ["PENDING_PAYMENT", "WAITING_FOR_MATCH", "AWAITING_PAYMENT"].includes(order.status)).length, "/orders"),
    metricCard("Under review", orders.filter(order => order.status === "PAYMENT_REVIEW").length, "/orders"),
    metricCard("Server-confirmed", orders.filter(order => ["PAID", "FULFILLED"].includes(order.status)).length, "/orders"),
  );
  content.append(metrics, sectionHead("Orders and payment state", "/orders", "All orders"));
  const list = el("div", "", "order-list");
  for (const order of orders) list.append(orderCard(order));
  content.append(list);
}

async function orderDetail() {
  const id = location.pathname.split("/").filter(Boolean).at(-1);
  const order = await get(`/api/v1/orders/${encodeURIComponent(id)}`, true);
  const content = clear();
  const grid = el("div", "", "detail-grid");
  const items = el("section", "", "detail-card");
  items.append(el("h2", "Order items"));
  for (const item of order.items) {
    const row = el("div", "", "line-item");
    row.append(el("span", `${item.product_name_snapshot} × ${item.quantity}`), el("span", money(item.line_total_paise, item.currency)));
    items.append(row);
  }
  for (const [name, amount] of [["Subtotal", order.subtotal_paise], ["Discount", order.discount_paise], ["Total", order.total_paise]]) {
    if (name === "Discount" && amount === 0) continue;
    const row = el("div", "", "line-item");
    row.append(el("span", name), el("span", money(amount, order.currency)));
    items.append(row);
  }
  const state = el("section", "", "detail-card");
  state.append(el("h2", "Status and delivery"), chip(order.status));
  const facts = document.createElement("dl");
  for (const [name, value] of [
    ["Order ID", order.id], ["Payment state", label(order.status)],
    ["Delivery", label(order.delivery_status)], ["Reservation deadline", date(order.expires_at)],
  ]) facts.append(el("dt", name), el("dd", value));
  state.append(facts);
  if (!finalStatuses.has(order.status)) state.append(link("View checkout status", `/checkout?order=${encodeURIComponent(order.id)}`, "button secondary"));
  grid.append(items, state);
  content.append(grid);
}

export async function render(page) {
  const user = await requireUser();
  if (!user) return;
  navigation(page);
  if (page === "dashboard") return dashboard(user);
  if (page === "tickets") return tickets();
  if (page === "orders") return orders();
  if (page === "payments") return payments();
  if (page === "order-detail") return orderDetail();
}
