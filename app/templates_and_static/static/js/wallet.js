import {button, clear, date, el, get, link, money, post, requireUser, showStatus, toPaise, withError} from "./api.js";
import {navigation} from "./account_v2.js";

const unresolved = new Set(["WAITING_FOR_BUYER", "MATCHED", "UNDER_REVIEW"]);

function balanceCard(title, value, note) {
  const node = el("section", "", "metric-card balance-card");
  node.append(el("p", title, "metric-label"), el("p", value, "metric-value"), el("p", note, "balance-note"));
  return node;
}
function heading(title, note) {
  const node = el("div", "", "section-head");
  const copy = el("div");
  copy.append(el("h2", title));
  if (note) copy.append(el("p", note, "muted small"));
  node.append(copy);
  return node;
}
function statusChip(value) {
  const node = el("span", value.replaceAll("_", " ").toLowerCase().replace(/\b\w/g, c => c.toUpperCase()), "status-chip");
  node.dataset.tone = value === "COMPLETED" ? "good" : value === "UNDER_REVIEW" ? "review" : value === "CANCELLED" ? "muted" : "default";
  return node;
}

async function walletPage() {
  const [wallet, rows] = await Promise.all([
    get("/api/v1/wallet", true), get("/api/v1/wallet/transactions", true),
  ]);
  const root = clear();
  if (!wallet) {
    const node = el("section", "", "card wallet-empty");
    node.append(el("h2", "No wallet yet"));
    node.append(el("p", "Create an empty wallet to see balances and prize credits. This action does not add money.", "muted"));
    node.append(button("Create empty wallet", withError(async () => {
      await post("/api/v1/wallet/provision");
      showStatus("Empty wallet created. No funds were added.", "success");
      await walletPage();
    })));
    root.append(node);
    return;
  }
  const metrics = el("div", "", "metric-grid wallet-metrics");
  metrics.append(
    balanceCard("Available", money(wallet.available_paise, wallet.currency), "Funds not currently held"),
    balanceCard("Held", money(wallet.locked_paise, wallet.currency), "Reserved by unresolved operations"),
    balanceCard("Total", money(wallet.total_paise, wallet.currency), "Available plus held"),
  );
  const action = el("section", "", "next-step wallet-next");
  const copy = el("div");
  copy.append(el("p", "Wallet action", "eyebrow"), el("h2", "Need to request a withdrawal?"));
  copy.append(el("p", "The server checks your eligible amount and verified destination before holding funds."));
  action.append(copy, link("View withdrawals", "/withdrawals", "button"));
  root.append(metrics, action, heading("Wallet activity", "Ledger entries are shown as recorded by the server."));
  if (!rows.length) {
    root.append(el("p", "No wallet activity yet.", "empty-state"));
    return;
  }
  const list = el("ul", "", "ledger-list");
  for (const row of rows) {
    const item = el("li", "", "ledger-entry");
    const copy = el("div");
    copy.append(el("strong", row.event_type.replaceAll("_", " ")));
    copy.append(el("p", `${row.account_kind} · ${row.direction} · ${date(row.created_at)}`, "muted small"));
    item.append(copy, el("strong", money(row.amount_paise, row.currency), "ledger-amount"));
    list.append(item);
  }
  root.append(list);
}

function requestForm(destinations) {
  const form = el("form", "", "card form-card withdrawal-form");
  form.append(el("h2", "Request a withdrawal"));
  form.append(el("p", "The server checks eligibility, the configured cap and your verified destination before holding funds. This is not an immediate payout.", "muted"));
  const amountWrap = el("label", "", "field");
  amountWrap.append(el("span", "Amount in rupees"));
  const amount = document.createElement("input");
  amount.name = "amount"; amount.type = "text"; amount.inputMode = "decimal";
  amount.placeholder = "0.00"; amount.required = true;
  amountWrap.append(amount);
  const destinationWrap = el("label", "", "field");
  destinationWrap.append(el("span", "Operations-verified destination"));
  const select = document.createElement("select");
  select.name = "destination"; select.required = true;
  for (const destination of destinations) {
    const option = el("option", `${destination.display_label} · ${destination.provider_namespace}`);
    option.value = destination.id; select.append(option);
  }
  destinationWrap.append(select);
  const submit = el("button", "Request withdrawal", "button");
  submit.type = "submit";
  form.append(amountWrap, destinationWrap, submit);
  form.addEventListener("submit", withError(async event => {
    event.preventDefault();
    const amount_paise = toPaise(amount.value.trim());
    submit.disabled = true;
    try {
      const withdrawal = await post("/api/v1/withdrawals", {
        amount_paise, payment_destination_id: select.value,
      });
      showStatus(`Request ${withdrawal.status}. Funds are held, not paid out.`, "success");
      await withdrawalsPage();
    } finally { submit.disabled = false; }
  }));
  return form;
}

function withdrawalCard(withdrawal, destinations) {
  const node = el("article", "", "withdrawal-card");
  const top = el("div", "", "withdrawal-top");
  top.append(el("h3", money(withdrawal.amount_paise, withdrawal.currency)), statusChip(withdrawal.status));
  node.append(top);
  node.append(el("p", `Request ${withdrawal.id}`, "muted small"));
  const destination = destinations.find(item => item.id === withdrawal.payment_destination_id);
  if (destination) node.append(el("p", `Verified destination: ${destination.display_label}`, "small"));
  const details = document.createElement("details");
  details.append(el("summary", "View request details"));
  details.append(el("p", `Eligible balance at request: ${money(withdrawal.eligible_balance_snapshot_paise, withdrawal.currency)}`));
  details.append(el("p", `Maximum at request: ${money(withdrawal.max_amount_snapshot_paise, withdrawal.currency)}`));
  if (withdrawal.matched_at) details.append(el("p", `Matched ${date(withdrawal.matched_at)}`));
  if (withdrawal.completed_at) details.append(el("p", `Completed ${date(withdrawal.completed_at)}`));
  if (withdrawal.cancelled_at) details.append(el("p", `Cancelled ${date(withdrawal.cancelled_at)}`));
  node.append(details);
  node.append(link("View timeline", `/withdrawals/${encodeURIComponent(withdrawal.id)}`, "button secondary"));
  if (withdrawal.status === "WAITING_FOR_BUYER") {
    node.append(button("Cancel unmatched request", withError(async () => {
      if (!confirm("Cancel this unmatched withdrawal request?")) return;
      await post(`/api/v1/withdrawals/${encodeURIComponent(withdrawal.id)}/cancel`);
      showStatus("Withdrawal cancelled; the server released its hold.", "success");
      await withdrawalsPage();
    }), "button danger"));
  }
  return node;
}

async function withdrawalsPage() {
  const [wallet, withdrawals, destinations] = await Promise.all([
    get("/api/v1/wallet", true), get("/api/v1/withdrawals", true),
    get("/api/v1/withdrawals/destinations", true),
  ]);
  const root = clear();
  const metrics = el("div", "", "metric-grid withdrawal-metrics");
  metrics.append(
    balanceCard("Available wallet", wallet ? money(wallet.available_paise, wallet.currency) : "Not provisioned", "Current server balance"),
    balanceCard("Held wallet", wallet ? money(wallet.locked_paise, wallet.currency) : "Not provisioned", "Current server holds"),
    balanceCard("Eligible maximum", "Checked on submit", "No client-side estimate"),
  );
  root.append(metrics);
  const active = withdrawals.find(item => unresolved.has(item.status));
  if (!wallet) root.append(el("p", "Create a wallet before requesting a withdrawal.", "notice"), link("Open wallet", "/wallet", "button secondary"));
  else if (active) root.append(el("p", `Request ${active.id} remains ${active.status.replaceAll("_", " ")}. Only one unresolved withdrawal is allowed.`, "notice"));
  else if (!destinations.length) root.append(el("p", "A verified payment destination is required. Verification is handled by operations; this page cannot self-verify a destination.", "notice"));
  else root.append(requestForm(destinations));
  root.append(heading("Request history", "A claim, match or hold is not a completed payout."));
  if (!withdrawals.length) {
    root.append(el("p", "No withdrawal requests yet.", "empty-state"));
    return;
  }
  const list = el("div", "", "withdrawal-list");
  for (const withdrawal of withdrawals) list.append(withdrawalCard(withdrawal, destinations));
  root.append(list);
}

export async function render(page) {
  if (!await requireUser()) return;
  navigation(page);
  if (page === "wallet") return walletPage();
  if (page === "withdrawals") return withdrawalsPage();
}
