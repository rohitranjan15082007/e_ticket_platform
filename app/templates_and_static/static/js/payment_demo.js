import {clear, el, link} from "./api.js";

// This module is presentation-only. It never calls a payment or order endpoint.
const methods = [
  {
    id: "manual",
    label: "Manual UPI",
    badge: "Evidence review",
    title: "Pay with an approved UPI destination",
    description: "A real checkout would show an administrator-approved destination after the server creates a payment attempt. A UTR or screenshot starts review; it never confirms payment by itself.",
    steps: ["Review the server-priced amount and approved payee.", "Pay outside this site and submit the actual amount and reference.", "Wait for independent review and a confirmed settlement."],
    note: "No UPI ID or QR code is shown in this demo.",
  },
  {
    id: "p2p",
    label: "P2P transfer",
    badge: "Exact match",
    title: "Match an eligible withdrawal",
    description: "The system finds a compatible withdrawal for the exact order amount. Buyer and receiver see frozen instructions; a payment claim alone does not settle the order.",
    steps: ["Wait for an exact eligible match.", "Use only the destination shown by the server for that match.", "Provider evidence and the required confirmation or review complete settlement."],
    note: "No receiver or payment destination is assigned in this demo.",
  },
  {
    id: "stars",
    label: "Telegram Stars",
    badge: "Bot checkout",
    title: "Complete an invoice in Telegram",
    description: "A separately operated bot must send the invoice. Pre-checkout approval is not payment; only a validated successful-payment update can settle an order.",
    steps: ["Prepare a signed invoice for the real order.", "Open and pay that invoice in the configured Telegram bot.", "Wait for the authenticated successful-payment update."],
    note: "This demo does not open Telegram, send an invoice, or charge Stars.",
  },
  {
    id: "provider",
    label: "Provider",
    badge: "Integration pending",
    title: "Use a connected payment provider",
    description: "A white-label provider can appear here only after its adapter, credentials, signing rules and reconciliation are configured and tested.",
    steps: ["Create an order using the live catalog.", "Start the configured provider checkout.", "Trust only a signed, deduplicated provider confirmation."],
    note: "Provider initiation is not configured in this demo.",
  },
];

function section(title, className = "") {
  const node = el("section", "", className);
  node.append(el("h2", title));
  return node;
}

function orderSummary() {
  const aside = el("aside", "", "demo-summary");
  aside.setAttribute("aria-label", "Sample order summary");
  const heading = el("div", "", "summary-heading");
  heading.append(el("span", "SAMPLE ORDER", "sample-tag"), el("h2", "Your order"));
  aside.append(heading);
  const item = el("div", "", "summary-item");
  item.append(el("span", "Example ticket × 1"), el("strong", "INR 250.00"));
  aside.append(item);
  const total = el("div", "", "summary-total");
  total.append(el("span", "Sample total"), el("strong", "INR 250.00"));
  aside.append(total);
  aside.append(el("p", "Illustrative amount only. No order exists, no money is collected, and no ticket is reserved.", "summary-disclaimer"));
  const safety = section("Before you pay", "demo-safety");
  safety.append(el("p", "In a real checkout, confirm the amount and destination shown by the server. Payment evidence is not proof of settlement."));
  aside.append(safety);
  return aside;
}

function progress() {
  const list = el("ol", "", "demo-progress");
  ["Choose tickets", "Review order", "Choose payment"].forEach((label, index) => {
    const item = el("li", "", index === 2 ? "current" : "complete");
    item.append(el("span", String(index + 1), "progress-number"), el("span", label));
    list.append(item);
  });
  list.setAttribute("aria-label", "Checkout steps shown for layout preview");
  return list;
}

export async function render() {
  const root = clear();
  const layout = el("div", "", "payment-layout");
  const workspace = el("div", "", "payment-workspace");
  workspace.append(progress());
  const picker = section("Payment methods", "method-picker");
  picker.append(el("p", "Select a method to see its real-world verification path. These controls only change this demo view.", "muted"));
  const tabs = el("div", "", "method-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Payment method previews");
  const panel = el("section", "", "method-panel");
  panel.id = "method-panel";
  panel.setAttribute("role", "tabpanel");
  const controls = methods.map((method, index) => {
    const control = el("button", "", "method-tab");
    control.type = "button";
    control.id = `method-tab-${method.id}`;
    control.setAttribute("role", "tab");
    control.setAttribute("aria-controls", panel.id);
    control.append(el("span", String(index + 1).padStart(2, "0"), "method-index"), el("span", method.label));
    tabs.append(control);
    return control;
  });
  function select(index, focus = false) {
    const method = methods[index];
    controls.forEach((control, controlIndex) => {
      const selected = controlIndex === index;
      control.setAttribute("aria-selected", String(selected));
      control.tabIndex = selected ? 0 : -1;
    });
    panel.setAttribute("aria-labelledby", controls[index].id);
    panel.replaceChildren();
    const top = el("div", "", "method-panel-top");
    top.append(el("span", method.badge, "method-badge"), el("span", "DEMO VIEW", "demo-mini-tag"));
    panel.append(top, el("h3", method.title), el("p", method.description, "method-description"));
    const list = el("ol", "", "method-steps");
    method.steps.forEach(step => list.append(el("li", step)));
    panel.append(list, el("p", method.note, "method-note"));
    if (focus) controls[index].focus();
  }
  controls.forEach((control, index) => {
    control.addEventListener("click", () => select(index));
    control.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? controls.length - 1 :
        (index + (event.key === "ArrowRight" ? 1 : -1) + controls.length) % controls.length;
      select(next, true);
    });
  });
  picker.append(tabs, panel);
  workspace.append(picker);
  const foot = el("div", "", "demo-next");
  foot.append(el("p", "Ready to explore the real product flow? Start with the live catalog. No payment method is activated by this demo."));
  foot.append(link("Browse live catalog", "/series", "button"));
  workspace.append(foot);
  layout.append(workspace, orderSummary());
  root.append(layout);
  select(0);
}
