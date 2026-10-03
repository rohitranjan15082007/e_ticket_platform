import {clear, date, el, get, link, money, requireUser} from "./api.js";
import {renderAdminActions} from "./admin_actions.js";

const groups = [
  ["Overview", [["Dashboard", "/admin"], ["Admin actions", "/admin/actions"]]],
  ["People", [["Users", "/admin/users"]]],
  ["Catalog", [["Series", "/admin/series"], ["Packages", "/admin/packages"], ["Draws & results", "/admin/draws"]]],
  ["Operations", [["Payments", "/admin/payments"], ["Withdrawals & P2P", "/admin/withdrawals"], ["Disputes & refunds", "/admin/disputes"]]],
  ["Business", [["Revenue", "/admin/revenue"], ["Marketing", "/admin/marketing"], ["Audit", "/admin/audit"]]],
];
const path = location.pathname;
const readable = value => String(value ?? "Unavailable").replaceAll("_", " ").toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
function chrome(title, subtitle, active = path) {
  const sidebar = document.getElementById("admin-sidebar");
  sidebar.replaceChildren();
  const toggle = el("button", "Operations sections", "admin-menu-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.addEventListener("click", () => {
    const open = sidebar.classList.toggle("is-open");
    toggle.setAttribute("aria-expanded", String(open));
  });
  sidebar.append(toggle);
  for (const [group, entries] of groups) {
    sidebar.append(el("h2", group));
    const nav = el("nav"); nav.setAttribute("aria-label", `${group} sections`);
    for (const [label, href] of entries) {
      const anchor = link(label, href);
      if (active === href) anchor.setAttribute("aria-current", "page");
      nav.append(anchor);
    }
    sidebar.append(nav);
  }
  document.getElementById("admin-heading").replaceChildren(
    el("p", "OPERATIONS / E-TICKET", "eyebrow"), el("h1", title), el("p", subtitle),
  );
}
function note(message) { return el("p", message, "notice"); }
function metric(label, value, href) {
  const node = el("section", "", "ops-card");
  node.append(el("h2", label), el("p", value, "metric"));
  if (href) node.append(link("Open section", href));
  return node;
}
function record(title, status, facts, href) {
  const node = el("article", "", "ops-record");
  const top = el("div", "", "ops-record-head");
  top.append(el("h2", title));
  if (status) top.append(el("span", readable(status), "ops-chip"));
  node.append(top);
  for (const fact of facts.filter(Boolean)) node.append(el("p", fact));
  if (href) node.append(link("View details", href, "button secondary"));
  return node;
}
function factsCard(title, pairs) {
  const node = el("section", "", "ops-record");
  node.append(el("h2", title));
  const list = el("dl", "", "ops-facts");
  for (const [label, value] of pairs) {
    if (value === null || value === undefined || value === "") continue;
    list.append(el("dt", label), el("dd", String(value)));
  }
  node.append(list);
  return node;
}
function listing(items, make, emptyText, before = []) {
  const root = clear(), toolbar = el("div", "", "ops-toolbar"), label = el("label");
  label.append(el("span", "Search loaded records"));
  const search = el("input"); search.type = "search"; search.placeholder = "Name, ID or status";
  label.append(search); toolbar.append(label);
  const count = el("p", "", "muted small"), list = el("div", "", "ops-records");
  function update() {
    const term = search.value.trim().toLowerCase();
    const visible = term ? items.filter(item => JSON.stringify(item).toLowerCase().includes(term)) : items;
    count.textContent = `Showing ${visible.length} of ${items.length} loaded records`;
    list.replaceChildren();
    if (!visible.length) list.append(el("p", term ? "No records match this search." : emptyText, "empty-state"));
    else for (const item of visible) list.append(make(item));
  }
  search.addEventListener("input", update);
  root.append(...before, toolbar, count, list); update();
}
function back(label, href) { return el("div", "", "ops-actions").appendChild(link(label, href, "button secondary")); }
function catalogActionPanel(title, description, action, label, secondary = []) {
  const panel = el("section", "", "ops-record catalog-action-panel");
  panel.append(el("h2", title), el("p", description));
  const actions = el("div", "", "ops-actions");
  actions.append(link(label, `/admin/actions?action=${encodeURIComponent(action)}`, "button"));
  for (const [secondaryLabel, href] of secondary) actions.append(link(secondaryLabel, href, "button secondary"));
  panel.append(actions);
  return panel;
}
async function dashboard() {
  chrome("Operations overview", "Server counts are triage signals, never payment verification.", "/admin");
  const data = await get("/api/v1/admin/dashboard/summary", true);
  const grid = el("div", "", "ops-grid");
  for (const [label, value, href] of [
    ["Total users", data.total_users, "/admin/users"],
    ["Open series", data.open_series, "/admin/series"],
    ["Closed series", data.closed_series, "/admin/series"],
    ["Tickets sold", data.tickets_sold, "/admin/series"],
    ["Tickets remaining", data.open_tickets_remaining, "/admin/series"],
    ["Pending orders", data.pending_orders, "/admin/payments"],
    ["Manual proofs to review", data.pending_manual_proofs, "/admin/payments"],
    ["Active P2P matches", data.active_p2p_matches, "/admin/withdrawals"],
    ["Open disputes", data.open_disputes, "/admin/disputes"],
    ["Unresolved withdrawals", data.unresolved_withdrawals, "/admin/withdrawals"],
    ["Referral rewards to review", data.pending_referral_rewards, "/admin/marketing"],
    ["Cashback rewards to review", data.pending_cashback_rewards, "/admin/marketing"],
    ["Affiliate commissions to review", data.pending_affiliate_commissions, "/admin/marketing"],
    ["Allocated revenue", money(data.allocated_revenue_paise), "/admin/revenue"],
  ]) grid.append(metric(label, value, href));
  clear().append(note("Review independent evidence before financial decisions. A count or claim is not settlement."), grid);
}
async function series() {
  chrome("Ticket series", "Inventory, lifecycle and pricing from the administrator API.", "/admin/series");
  const items = await get("/api/v1/admin/ticket-series", true);
  listing(items, item => record(item.name, item.status, [
    `Series ID ${item.id}`,
    `${money(item.price_paise)} per ticket · Sold ${item.sold_count} · Reserved ${item.reserved_count} · Limit ${item.ticket_limit}`,
    `Sales end ${date(item.sales_end_at)} · Draw ${date(item.draw_at)}`,
  ], `/admin/catalog/series/${encodeURIComponent(item.id)}`), "No ticket series yet. Create the first draft above.", [
    catalogActionPanel(
      "Create a ticket series",
      "Create a DRAFT with server-validated price, dates and prizes. Review it, then publish it before adding it to a package.",
      "series-create",
      "Create series",
    ),
    note("Catalog workflow: create DRAFT → review details → publish → optionally add to a package. Opening sales has additional draw-commitment and date-window safeguards."),
  ]);
}
async function packages() {
  chrome("Packages", "Administrator catalog, including inactive package inventory.", "/admin/packages");
  const [items, seriesItems] = await Promise.all([
    get("/api/v1/admin/ticket-packages", true), get("/api/v1/admin/ticket-series", true),
  ]);
  const eligible = seriesItems.filter(item => ["PUBLISHED", "OPEN"].includes(item.status));
  const packageHelp = eligible.length
    ? `${eligible.length} published/open series ${eligible.length === 1 ? "is" : "are"} available. Copy a Series ID from the Series page into the package form.`
    : "No series is eligible yet. Create a series and publish it first; DRAFT series cannot be added to packages.";
  const packageAction = eligible.length ? "package-create" : "series-create";
  const packageLabel = eligible.length ? "Create package" : "Create series first";
  listing(items, item => record(item.name, item.is_active ? "Active" : "Inactive", [
    `${money(item.price_paise)} · ${item.items.length} series · ${item.sold_count} sold`, item.description,
  ], `/admin/catalog/packages/${encodeURIComponent(item.id)}`), "No packages yet. Follow the guided workflow above.", [
    catalogActionPanel(
      "Create a ticket package",
      packageHelp,
      packageAction,
      packageLabel,
      eligible.length ? [["View eligible series", "/admin/series"]] : [["Open package form", "/admin/actions?action=package-create"]],
    ),
    note("Package rule: every item must reference a PUBLISHED or OPEN series. Creating a series leaves it in DRAFT until an administrator publishes it."),
  ]);
}
async function draws() {
  chrome("Draws & results", "Inspect the server-controlled lifecycle; this browser cannot choose winners.", "/admin/draws");
  const items = await get("/api/v1/admin/ticket-series", true);
  listing(items, item => record(item.name, item.status, [
    `Series ${item.id} · Draw scheduled ${date(item.draw_at)}`,
  ], `/admin/draws/${encodeURIComponent(item.id)}`), "No series to inspect.");
}
async function payments() {
  chrome("Payment review", "Manual UPI claims need independent verification.", "/admin/payments");
  const items = await get("/api/v1/admin/manual-upi/review", true);
  const root = clear(); root.append(note("A UTR or screenshot alone must never approve payment. This is only the manual UPI queue."));
  const list = el("div", "", "ops-records");
  if (!items.length) list.append(el("p", "No manual UPI proofs awaiting review.", "empty-state"));
  for (const item of items) list.append(record(`Proof ${item.id}`, item.status, [
    `Claimed ${money(item.submitted_amount_paise)} · Attempt ${item.payment_attempt_id}`,
    `UTR ${item.utr} · Evidence reference ${item.proof_reference}`,
  ], `/admin/payments/${encodeURIComponent(item.id)}`));
  root.append(list);
}
async function withdrawals() {
  chrome("Withdrawals & P2P", "Held funds, matching and reconciliation require server-controlled review.", "/admin/withdrawals");
  const items = await get("/api/v1/admin/withdrawals", true);
  listing(items, item => record(money(item.amount_paise), item.status, [
    `Request ${item.id} · User ${item.user_id}`,
    `Rule ${item.rule_version} · Cap at request ${money(item.max_amount_snapshot_paise)}`,
  ]), "No withdrawal requests.");
}
async function disputes() {
  chrome("Disputes & refunds", "A dispute case is not a completed refund or payout.", "/admin/disputes");
  const items = await get("/api/v1/admin/disputes", true);
  listing(items, item => record(readable(item.reason_code), item.status, [
    `Dispute ${item.id} · Match ${item.match_id}`,
    `Opened ${date(item.created_at)} · Evidence ${item.evidence_reference || "none"}`,
  ], `/admin/disputes/${encodeURIComponent(item.id)}`), "No disputes.");
}
async function revenue() {
  chrome("Revenue", "Recorded integer-paise allocations, not a bank settlement report.", "/admin/revenue");
  const [report, allocations] = await Promise.all([
    get("/api/v1/admin/revenue/report", true), get("/api/v1/admin/revenue/allocations?limit=50", true),
  ]);
  const grid = el("div", "", "ops-grid");
  for (const [label, amount] of [
    ["Allocation base", report.allocation_base_paise], ["Prize pool", report.prize_pool_paise],
    ["Marketing", report.marketing_paise], ["Operations", report.operations_paise],
    ["Emergency reserve", report.reserve_paise], ["Profit / growth", report.profit_growth_paise],
  ]) grid.append(metric(label, money(amount)));
  const root = clear();
  root.append(note(`${report.allocation_count} recorded allocations · ${report.currency}`), grid, el("h2", "Recent allocations"));
  const list = el("div", "", "ops-records");
  if (!allocations.length) list.append(el("p", "No allocations.", "empty-state"));
  for (const item of allocations) list.append(record(money(item.allocation_base_paise), item.source, [
    `Order ${item.order_id} · ${date(item.allocated_at)}`,
  ]));
  root.append(list);
}
async function marketing() {
  chrome("Marketing", "Program states and pending rewards; pending is not wallet credit.", "/admin/marketing");
  const endpoints = [
    ["Coupons", "coupons"], ["Referral programs", "referral-programs"], ["Cashback campaigns", "cashback-campaigns"],
    ["Affiliates", "affiliates"], ["Referral rewards", "referral-rewards"],
    ["Cashback rewards", "cashback-rewards"], ["Affiliate conversions", "affiliate-conversions"],
    ["Affiliate commissions", "affiliate-commissions"],
  ];
  const results = await Promise.all(endpoints.map(async ([label, slug]) =>
    [label, await get(`/api/v1/admin/marketing/${slug}`, true)]));
  const grid = el("div", "", "ops-grid");
  for (const [label, items] of results) grid.append(metric(label, items.length));
  clear().append(note("Review records are not wallet credits or executed payouts."), grid);
}
async function audit() {
  chrome("Audit", "Bounded, redacted event metadata. Secret payloads are excluded.", "/admin/audit");
  const items = await get("/api/v1/admin/audit?limit=50", true);
  listing(items, item => record(readable(item.action), item.entity_type, [
    `Event ${item.id} · ${date(item.created_at)}`,
    `Actor ${item.actor_user_id || "system"} · Entity ${item.entity_id}`,
  ]), "No audit events in the newest 50 records.");
}
async function users() {
  chrome("Users", "Bounded account list with masked email. Role changes are not exposed here.", "/admin/users");
  const items = await get("/api/v1/admin/users?limit=50", true);
  listing(items, item => record(item.email_hint, item.is_active ? "Active" : "Inactive", [
    `User ${item.id} · ${item.full_name || "No name provided"}`,
    `Roles ${item.roles.join(", ") || "none"} · Created ${date(item.created_at)}`,
  ], `/admin/users/${encodeURIComponent(item.id)}`), "No users in the newest 50 records.");
}
async function detail() {
  const parts = path.split("/").filter(Boolean), area = parts[1], id = parts.at(-1);
  if (area === "catalog" && parts[2] === "series") {
    chrome("Series record", "Read-only catalog state and draw context.", "/admin/series");
    const item = await get(`/api/v1/admin/ticket-series/${encodeURIComponent(id)}`, true);
    const actions = el("div", "", "ops-actions");
    actions.append(link("Edit series", `/admin/actions?action=series-update&series_id=${encodeURIComponent(id)}`, "button secondary"));
    if (item.status === "DRAFT") {
      actions.append(link("Publish series", `/admin/actions?action=series-publish&series_id=${encodeURIComponent(id)}`, "button"));
    }
    if (["PUBLISHED", "OPEN"].includes(item.status)) {
      actions.append(link("Create package with this series", `/admin/actions?action=package-create&series_id=${encodeURIComponent(id)}`, "button"));
    }
    clear().append(back("Back to series", "/admin/series"), actions, factsCard(item.name, [
      ["Status", readable(item.status)], ["ID", item.id], ["Price", money(item.price_paise)],
      ["Sold", item.sold_count], ["Reserved", item.reserved_count], ["Limit", item.ticket_limit],
      ["Sales end", date(item.sales_end_at)], ["Draw", date(item.draw_at)],
    ]));
  } else if (area === "catalog" && parts[2] === "packages") {
    chrome("Package record", "Public snapshot; the server controls changes and checkout.", "/admin/packages");
    const item = await get(`/api/v1/admin/ticket-packages/${encodeURIComponent(id)}`, true);
    const root = clear();
    root.append(back("Back to packages", "/admin/packages"), factsCard(item.name, [
      ["ID", item.id], ["Price", money(item.price_paise)], ["Active", item.is_active ? "Yes" : "No"],
      ["Inventory limit", item.inventory_limit ?? "Unlimited"], ["Sold", item.sold_count],
    ]));
    for (const line of item.items) root.append(record(`Series ${line.series_id}`, null, [`Quantity ${line.quantity}`]));
  } else if (area === "draws") {
    chrome("Draw record", "Commit, reveal and publication are controlled and audited by the server.", "/admin/draws");
    const item = await get(`/api/v1/admin/ticket-series/${encodeURIComponent(id)}/draw`, true);
    clear().append(back("Back to draws", "/admin/draws"), factsCard("Draw snapshot",
      Object.entries(item).filter(([, value]) => value === null || typeof value !== "object")
        .map(([key, value]) => [readable(key), value])),
      note("This read-only view cannot choose winners or bypass draw prerequisites."));
  } else if (area === "payments") {
    chrome("Payment case", "Manual proof is a claim, not settlement.", "/admin/payments");
    const items = await get("/api/v1/admin/manual-upi/review", true);
    const item = items.find(value => value.id === id);
    clear().append(back("Back to payments", "/admin/payments"), item ? factsCard("Manual proof", [
      ["Proof ID", item.id], ["Attempt", item.payment_attempt_id], ["Status", readable(item.status)],
      ["Claimed amount", money(item.submitted_amount_paise)], ["UTR", item.utr],
      ["Evidence reference", item.proof_reference],
    ]) : note("This proof is not in the pending-review queue. A case-detail read API is unavailable."));
  } else if (area === "disputes") {
    chrome("Dispute case", "Recorded evidence and state; no automatic fund release.", "/admin/disputes");
    const items = await get("/api/v1/admin/disputes", true);
    const item = items.find(value => value.id === id);
    clear().append(back("Back to disputes", "/admin/disputes"), item ? factsCard("Dispute snapshot", [
      ["Dispute ID", item.id], ["Match", item.match_id], ["Status", readable(item.status)],
      ["Reason", readable(item.reason_code)], ["Evidence", item.evidence_reference || "None"],
      ["Opened", date(item.created_at)],
    ]) : note("This dispute was not returned by the current list endpoint."));
  } else if (area === "users") {
    chrome("User record", "Read-only masked identity and role snapshot.", "/admin/users");
    const item = await get(`/api/v1/admin/users/${encodeURIComponent(id)}`, true);
    clear().append(back("Back to users", "/admin/users"), factsCard("Account", [
      ["User ID", item.id], ["Email", item.email_hint], ["Name", item.full_name || "Not provided"],
      ["Active", item.is_active ? "Yes" : "No"], ["Verified", item.is_verified ? "Yes" : "No"],
      ["Roles", item.roles.join(", ") || "None"], ["Created", date(item.created_at)],
    ]), note("This view does not change roles, verification or account status."));
  }
}
export async function render() {
  if (!await requireUser(true)) return;
  const main = {
    "/admin": dashboard, "/admin/users": users, "/admin/series": series,
    "/admin/packages": packages, "/admin/draws": draws, "/admin/payments": payments,
    "/admin/withdrawals": withdrawals, "/admin/disputes": disputes,
    "/admin/revenue": revenue, "/admin/marketing": marketing, "/admin/audit": audit,
    "/admin/actions": () => renderAdminActions(chrome),
  };
  return (main[path] || detail)();
}
