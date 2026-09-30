import {clear, date, el, get, link, money, token} from "./api.js";

const path = location.pathname, parts = path.split("/").filter(Boolean);
function heading(title, description) {
  document.title = `${title} · E-Ticket`;
  document.getElementById("content-heading").replaceChildren(
    el("p", "E-TICKET / EXPLORE", "eyebrow"), el("h1", title), el("p", description),
  );
}
function panel(title, paragraphs = []) {
  const node = el("section", "", "content-panel");
  node.append(el("h2", title));
  for (const line of paragraphs) node.append(el("p", line));
  return node;
}
function facts(title, pairs) {
  const node = panel(title), list = el("dl");
  for (const [label, value] of pairs) {
    if (value === null || value === undefined || value === "") continue;
    list.append(el("dt", label), el("dd", String(value)));
  }
  node.append(list); return node;
}
function actions(entries) {
  const node = el("div", "", "content-actions");
  for (const [label, href] of entries) node.append(link(label, href, "button secondary"));
  return node;
}
function warning(title, message) {
  const node = el("section", "", "content-warning");
  node.append(el("h2", title), el("p", message)); return node;
}
async function seriesDetail(id) {
  const item = await get(`/api/v1/catalog/series/${encodeURIComponent(id)}`);
  heading(item.name, item.description || "Series information from the public catalog.");
  const remaining = Math.max(0, item.ticket_limit - item.sold_count - item.reserved_count);
  const main = facts("Series details", [
    ["Ticket price", money(item.price_paise, item.currency)], ["Status", item.status],
    ["Available now", remaining], ["Ticket limit", item.ticket_limit],
    ["Sales end", date(item.sales_end_at)], ["Draw scheduled", date(item.draw_at)],
    ["Commitment", item.draw_seed_commitment || "Not yet published"],
  ]);
  const list = el("ul", "", "content-list");
  for (const prize of item.prizes) list.append(el("li", `Rank ${prize.rank}: ${prize.title} · ${money(prize.prize_paise)}`));
  const side = panel("Prize tiers", ["Prize information is the series snapshot. Published results come from the draw service."]);
  side.append(list);
  const grid = el("div", "", "content-grid"); grid.append(main, side);
  const root = clear();
  root.append(actions([["All series", "/series"], ["Public results", "/results"]]), grid);
  if (item.status === "OPEN" && remaining > 0) root.append(actions([["Review at checkout", `/checkout?type=SERIES&id=${encodeURIComponent(id)}`]]));
  root.append(warning("Before you buy", "The final price and inventory are checked again when the order is created. A reservation is not a paid ticket."));
}
async function packageDetail(id) {
  const item = await get(`/api/v1/catalog/packages/${encodeURIComponent(id)}`);
  heading(item.name, item.description || "Package composition from the public catalog.");
  const main = facts("Package details", [
    ["Listed price", money(item.price_paise, item.currency)], ["Active", item.is_active ? "Yes" : "No"],
    ["Inventory limit", item.inventory_limit ?? "No package-level limit"], ["Sold", item.sold_count],
    ["Reserved", item.reserved_count],
  ]);
  const list = el("ul", "", "content-list");
  for (const entry of item.items) {
    const row = el("li");
    row.append(link(`Series ${entry.series_id}`, `/series/${encodeURIComponent(entry.series_id)}`),
      el("span", ` · ${entry.quantity} ticket(s)`));
    list.append(row);
  }
  const side = panel("Included series", ["Each series remains subject to its own availability rules."]);
  side.append(list);
  const grid = el("div", "", "content-grid"); grid.append(main, side);
  const root = clear();
  root.append(actions([["All packages", "/packages"]]), grid);
  const remaining = item.inventory_limit === null ? 1 : item.inventory_limit - item.sold_count - item.reserved_count;
  if (item.is_active && remaining > 0) root.append(actions([["Review at checkout", `/checkout?type=PACKAGE&id=${encodeURIComponent(id)}`]]));
  root.append(warning("Before you buy", "The server checks package composition, price and inventory again at checkout. No payment is confirmed by selecting a package."));
}
async function resultDetail(id) {
  const item = await get(`/api/v1/results/${encodeURIComponent(id)}`);
  heading(item.series_name, `Published result · ${date(item.published_at)}`);
  const proof = facts("Draw verification", [
    ["Algorithm", item.algorithm_version], ["Commitment", item.seed_commitment],
    ["Revealed seed", item.seed_reveal], ["Eligible tickets", item.eligible_ticket_count],
    ["Candidate digest", item.eligible_tickets_digest], ["Result digest", item.result_digest],
    ["Drawn", date(item.drawn_at)],
  ]);
  const winners = panel("Published winners", ["Owner identities are not exposed here."]);
  const list = el("ul", "", "content-list");
  for (const winner of item.winners) list.append(el("li",
    `Rank ${winner.rank} · Ticket #${winner.ticket_serial_number} · ${winner.title} · ${money(winner.prize_paise)}`));
  if (!item.winners.length) list.append(el("li", "No winner records in this published result."));
  winners.append(list);
  const grid = el("div", "", "content-grid"); grid.append(proof, winners);
  clear().append(actions([["All results", "/results"]]), grid);
}
function policy(title, subject) {
  heading(title, "Publication pending owner review.");
  clear().append(warning("Not an approved policy",
    `The project does not contain owner-approved ${subject} wording. This page is intentionally not presented as binding terms. Supply and review the text before a public launch.`),
    actions([["How it works", "/how-it-works"], ["Back to home", "/"]]));
}
function howItWorks() {
  heading("How it works", "Explore, reserve, pay through an available method and check server-published results.");
  const root = clear(), list = el("div", "", "content-stack");
  for (const [title, body] of [
    ["01 · Explore", "Review a current series or package. Catalog prices and inventory can change."],
    ["02 · Reserve", "Checkout creates a server-priced order and checks availability. A reservation is not a paid ticket."],
    ["03 · Payment review", "The available method depends on configuration. Claims and uploaded references require independent confirmation."],
    ["04 · Ticket delivery", "The server delivers tickets only after confirmed settlement and allocation."],
    ["05 · Results", "Only published draw records are shown as results; decorative scratch-card styling does not determine a winner."],
  ]) list.append(panel(title, [body]));
  root.append(list, actions([["Browse series", "/series"], ["Browse packages", "/packages"]]));
}
async function paymentMethods() {
  heading("Payment methods", "Checkout presents methods that are configured for your order.");
  const root = clear();
  root.append(warning("Confirmation matters", "A UTR, screenshot, P2P claim or prepared invoice is not payment confirmation. Wait for the server-reported order state."));
  if (!token()) {
    root.append(panel("Sign in to check availability", ["Method availability depends on current configuration and can change before checkout."]),
      actions([["Sign in", "/login"], ["Explore packages", "/packages"]]));
    return;
  }
  let methods;
  try { methods = await get("/api/v1/payment-methods", true); }
  catch { root.append(panel("Availability unavailable", ["Please check again at checkout. No payment path should be inferred from this page."])); return; }
  const list = el("div", "", "content-stack");
  if (methods.p2p_match) list.append(panel("P2P matching", ["An exact match may queue until a qualifying receiver is available. A claim does not settle it."]));
  if (methods.manual_upi) list.append(panel("Manual UPI", ["Checkout supplies the approved destination. Submitted transaction references enter review only."]));
  if (methods.telegram_stars) list.append(panel("Telegram Stars", ["Invoice preparation is not payment. The configured bot and provider must confirm completion."]));
  if (!list.children.length) list.append(panel("No method available", ["Please return later; no payment method is currently configured."]));
  root.append(list);
}
export async function render() {
  if (parts[0] === "series" && parts[1]) return seriesDetail(parts[1]);
  if (parts[0] === "packages" && parts[1]) return packageDetail(parts[1]);
  if (parts[0] === "results" && parts[1]) return resultDetail(parts[1]);
  if (path === "/how-it-works") return howItWorks();
  if (path === "/payment-methods") return paymentMethods();
  const titles = {
    "/terms": ["Terms", "terms of use"], "/privacy": ["Privacy", "privacy notice"],
    "/refund-policy": ["Refund policy", "refund policy"], "/responsible-use": ["Responsible use", "responsible-use policy"],
  };
  if (titles[path]) return policy(...titles[path]);
}
