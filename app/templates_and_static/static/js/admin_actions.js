import {clear, el, errorMessage, post, put, showStatus} from "./api.js";

const field = (name, label, type = "text", options = {}) => ({name, label, type, ...options});
const idField = (name, label) => field(name, label, "uuid", {source: "path", required: true});
const reasonField = () => field("reason", "Reason", "textarea", {required: true, maxLength: 500});
const evidenceField = (name = "evidence_reference", label = "Evidence reference") =>
  field(name, label, "text", {required: true, maxLength: 255});
const paiseField = (name, label, required = true) =>
  field(name, label, "integer", {required, min: 1, help: "Whole INR paise; for example, 12500 means ₹125.00."});
const dateField = (name, label, required = false) => field(name, label, "datetime", {required});
const selectField = (name, label, options, required = true) => field(name, label, "select", {options, required});

const seriesId = () => idField("series_id", "Series ID");
const packageId = () => idField("package_id", "Package ID");
const lifecycle = (id, label, path, idFactory, confirm) => ({
  id, group: "Catalog lifecycle", label, method: "POST", path,
  description: "Run the audited server-side lifecycle transition.", fields: [idFactory()], confirm,
});
const marketingReview = (id, label, path, paramName, paramLabel, confirm) => ({
  id, group: "Marketing review", label, method: "POST", path,
  description: "Apply an audited review decision with a recorded reason.",
  fields: [idField(paramName, paramLabel), reasonField()], confirm, danger: true,
});

export const ADMIN_ACTIONS = [
  {
    id: "series-create", group: "Series", label: "Create ticket series", method: "POST",
    path: "/api/v1/admin/ticket-series", confirm: "CREATE SERIES",
    description: "Create a draft series. Dates are converted to timezone-aware ISO timestamps.",
    fields: [
      field("name", "Name", "text", {required: true, maxLength: 200}),
      field("description", "Description", "textarea", {required: false, maxLength: 10000}),
      paiseField("price_paise", "Ticket price"),
      field("ticket_limit", "Ticket limit", "integer", {required: true, min: 1, max: 10000000}),
      dateField("sales_start_at", "Sales start", true), dateField("sales_end_at", "Sales end", true),
      dateField("draw_at", "Draw time", true),
      field("prizes", "Prize definitions", "json", {
        required: true, jsonKind: "array",
        placeholder: '[{"rank":1,"title":"First prize","prize_paise":10000}]',
        help: "JSON array; every rank must be unique and every amount is integer paise.",
      }),
    ],
  },
  {
    id: "series-update", group: "Series", label: "Update draft series", method: "PUT",
    path: "/api/v1/admin/ticket-series/{series_id}", confirm: "UPDATE SERIES",
    description: "Send only fields that should change. Empty optional fields are omitted.",
    fields: [
      seriesId(), field("name", "Name", "text", {required: false, maxLength: 200}),
      field("description", "Description", "textarea", {required: false, maxLength: 10000, allowNull: true}),
      paiseField("price_paise", "Ticket price", false),
      field("ticket_limit", "Ticket limit", "integer", {required: false, min: 1, max: 10000000}),
      dateField("sales_start_at", "Sales start"), dateField("sales_end_at", "Sales end"),
      dateField("draw_at", "Draw time"),
      field("prizes", "Prize definitions", "json", {
        required: false, jsonKind: "array",
        placeholder: '[{"rank":1,"title":"First prize","prize_paise":10000}]',
      }),
    ],
  },
  lifecycle("series-publish", "Publish series", "/api/v1/admin/ticket-series/{series_id}/publish", seriesId, "PUBLISH SERIES"),
  lifecycle("series-open", "Open series sales", "/api/v1/admin/ticket-series/{series_id}/open", seriesId, "OPEN SALES"),
  lifecycle("series-close", "Close series sales", "/api/v1/admin/ticket-series/{series_id}/close", seriesId, "CLOSE SALES"),
  lifecycle("series-cancel", "Cancel series", "/api/v1/admin/ticket-series/{series_id}/cancel", seriesId, "CANCEL SERIES"),
  {
    id: "draw-commit", group: "Draws", label: "Commit draw seed", method: "POST",
    path: "/api/v1/admin/ticket-series/{series_id}/draw/commit", confirm: "COMMIT SEED", danger: true,
    description: "Commit the lowercase SHA-256 digest before sales close. Keep the secret reveal outside this browser.",
    fields: [seriesId(), field("seed_commitment", "Seed commitment", "text", {
      required: true, minLength: 64, maxLength: 64, pattern: "[0-9a-f]{64}",
      help: "Exactly 64 lowercase hexadecimal characters.",
    })],
  },
  {
    id: "draw-run", group: "Draws", label: "Reveal seed and run draw", method: "POST",
    path: "/api/v1/admin/ticket-series/{series_id}/draw/run", confirm: "RUN DRAW", danger: true,
    description: "Reveal the pre-committed seed and execute the deterministic draw once prerequisites pass.",
    fields: [seriesId(), field("seed_reveal", "Seed reveal", "password", {
      required: true, minLength: 16, maxLength: 512, autocomplete: "off",
      help: "Visible ASCII only. The server verifies it against the commitment.",
    })],
  },
  lifecycle("draw-post-prizes", "Post draw prizes", "/api/v1/admin/ticket-series/{series_id}/draw/post-prizes", seriesId, "POST PRIZES"),
  lifecycle("draw-publish", "Publish draw result", "/api/v1/admin/ticket-series/{series_id}/draw/publish", seriesId, "PUBLISH RESULT"),
  {
    id: "package-create", group: "Packages", label: "Create ticket package", method: "POST",
    path: "/api/v1/admin/ticket-packages", confirm: "CREATE PACKAGE",
    description: "Create a package that contains one or more existing ticket series.",
    fields: [
      field("name", "Name", "text", {required: true, maxLength: 200}),
      field("description", "Description", "textarea", {required: false, maxLength: 10000}),
      paiseField("price_paise", "Package price"),
      field("inventory_limit", "Inventory limit", "integer", {required: false, min: 1, max: 10000000}),
      field("items", "Package items", "json", {
        required: true, jsonKind: "array",
        placeholder: '[{"series_id":"00000000-0000-0000-0000-000000000000","quantity":1}]',
      }),
    ],
  },
  {
    id: "package-update", group: "Packages", label: "Update ticket package", method: "PUT",
    path: "/api/v1/admin/ticket-packages/{package_id}", confirm: "UPDATE PACKAGE",
    description: "Send only fields that should change. Use the clear control for nullable fields.",
    fields: [
      packageId(), field("name", "Name", "text", {required: false, maxLength: 200}),
      field("description", "Description", "textarea", {required: false, maxLength: 10000, allowNull: true}),
      paiseField("price_paise", "Package price", false),
      field("inventory_limit", "Inventory limit", "integer", {required: false, min: 1, max: 10000000, allowNull: true}),
      field("items", "Package items", "json", {
        required: false, jsonKind: "array",
        placeholder: '[{"series_id":"00000000-0000-0000-0000-000000000000","quantity":1}]',
      }),
    ],
  },
  lifecycle("package-deactivate", "Deactivate package", "/api/v1/admin/ticket-packages/{package_id}/deactivate", packageId, "DEACTIVATE PACKAGE"),
  {
    id: "match-close-unpaid", group: "Payments & P2P", label: "Close unpaid match", method: "POST",
    path: "/api/v1/admin/matches/{match_id}/close-unpaid", confirm: "CLOSE UNPAID", danger: true,
    description: "Close an unpaid P2P match using evidence. Releasing a hold is an explicit choice.",
    fields: [idField("match_id", "Match ID"), reasonField(), evidenceField(),
      field("release_hold", "Release withdrawal hold", "checkbox", {required: false, value: false})],
  },
  {
    id: "order-retry-delivery", group: "Payments & P2P", label: "Retry order delivery", method: "POST",
    path: "/api/v1/admin/orders/{order_id}/retry-delivery", confirm: "RETRY DELIVERY",
    description: "Retry the server-controlled delivery step for an already eligible order.",
    fields: [idField("order_id", "Order ID")],
  },
  {
    id: "manual-upi-destination", group: "Payments & P2P", label: "Create manual UPI destination", method: "POST",
    path: "/api/v1/admin/manual-upi/destinations", confirm: "CREATE UPI DESTINATION", danger: true,
    description: "Replace the active, audited manual UPI/QR instruction source.",
    fields: [
      field("display_label", "Display label", "text", {required: true, maxLength: 120}),
      field("upi_id", "UPI ID", "text", {required: true, maxLength: 255}),
      field("qr_reference", "QR asset/reference", "text", {required: false, maxLength: 255}),
      field("instructions", "Customer instructions", "json", {
        required: false, jsonKind: "object", placeholder: '{"note":"Pay the exact amount"}',
      }),
      evidenceField("approval_evidence_reference", "Approval evidence reference"),
    ],
  },
  {
    id: "manual-upi-review", group: "Payments & P2P", label: "Review manual UPI proof", method: "POST",
    path: "/api/v1/admin/manual-upi/{attempt_id}/review", confirm: "REVIEW PAYMENT", danger: true,
    description: "Approval may settle the payment. Verify bank-side evidence independently before submitting.",
    fields: [
      idField("attempt_id", "Payment attempt ID"),
      selectField("decision", "Decision", ["APPROVE", "REJECT", "KEEP_IN_REVIEW"]),
      field("review_note", "Review note", "textarea", {required: true, maxLength: 500}),
    ],
  },
  {
    id: "dispute-resolve", group: "Disputes & refunds", label: "Resolve P2P dispute", method: "POST",
    path: "/api/v1/admin/disputes/{dispute_id}/resolve", confirm: "RESOLVE DISPUTE", danger: true,
    description: "Evidence-backed settlement, closure or continued review. The server enforces valid transitions.",
    fields: [
      idField("dispute_id", "Dispute ID"),
      selectField("decision", "Decision", ["SETTLE", "CLOSE_UNPAID", "KEEP_IN_REVIEW"]),
      field("payment_submission_id", "Payment submission ID", "uuid", {required: false}),
      reasonField(), evidenceField(),
      field("verification_source", "Verification source", "text", {required: true, maxLength: 100}),
      field("release_hold", "Release withdrawal hold", "checkbox", {required: false, value: false}),
    ],
  },
  {
    id: "refund-create", group: "Disputes & refunds", label: "Create refund evidence case", method: "POST",
    path: "/api/v1/admin/refunds", confirm: "CREATE REFUND CASE", danger: true,
    description: "Record a pending evidence case only. This endpoint does not execute an external payout.",
    fields: [
      field("match_id", "Match ID", "uuid", {required: true}), paiseField("amount_paise", "Refund amount"),
      selectField("currency", "Currency", ["INR"]), reasonField(),
      field("liable_party", "Liable party", "text", {required: true, maxLength: 100}),
      evidenceField("funding_source_reference", "Funding source reference"),
      evidenceField("destination_validation_reference", "Destination validation reference"),
      evidenceField("executor_reference", "Executor reference"),
    ],
  },
  {
    id: "coupon-create", group: "Marketing", label: "Create coupon", method: "POST",
    path: "/api/v1/admin/marketing/coupons", confirm: "CREATE COUPON",
    description: "Create a fixed-paise or basis-point discount policy. Server validation prevents mixed definitions.",
    fields: [
      field("code", "Coupon code", "text", {required: true, minLength: 3, maxLength: 64}),
      field("description", "Description", "textarea", {required: false, maxLength: 500}),
      selectField("discount_type", "Discount type", ["FIXED_PAISE", "PERCENT_BPS"]),
      paiseField("fixed_discount_paise", "Fixed discount", false),
      field("percentage_bps", "Percentage (basis points)", "integer", {required: false, min: 1, max: 10000}),
      paiseField("max_discount_paise", "Maximum discount", false),
      field("minimum_order_paise", "Minimum order (paise)", "integer", {required: false, min: 0, value: 0}),
      field("usage_limit", "Global usage limit", "integer", {required: false, min: 1}),
      field("per_user_limit", "Per-user limit", "integer", {required: false, min: 1}),
      dateField("starts_at", "Starts at"), dateField("ends_at", "Ends at"),
    ],
  },
  marketingReview("coupon-deactivate", "Deactivate coupon", "/api/v1/admin/marketing/coupons/{coupon_id}/deactivate", "coupon_id", "Coupon ID", "DEACTIVATE COUPON"),
  {
    id: "referral-program-create", group: "Marketing", label: "Create referral program", method: "POST",
    path: "/api/v1/admin/marketing/referral-programs", confirm: "CREATE REFERRAL PROGRAM",
    description: "Create a draft referral reward policy.",
    fields: [
      field("name", "Name", "text", {required: true, maxLength: 120}),
      field("description", "Description", "textarea", {required: false, maxLength: 500}),
      paiseField("referrer_reward_paise", "Referrer reward"),
      paiseField("referred_reward_paise", "Referred-user reward"),
      paiseField("minimum_order_paise", "Minimum order"),
      dateField("starts_at", "Starts at"), dateField("ends_at", "Ends at"),
    ],
  },
  lifecycle("referral-program-activate", "Activate referral program", "/api/v1/admin/marketing/referral-programs/{program_id}/activate", () => idField("program_id", "Program ID"), "ACTIVATE REFERRAL PROGRAM"),
  marketingReview("referral-program-disable", "Disable referral program", "/api/v1/admin/marketing/referral-programs/{program_id}/disable", "program_id", "Program ID", "DISABLE REFERRAL PROGRAM"),
  marketingReview("referral-reward-void", "Void referral reward", "/api/v1/admin/marketing/referral-rewards/{reward_id}/void", "reward_id", "Reward ID", "VOID REFERRAL REWARD"),
  {
    id: "cashback-campaign-create", group: "Marketing", label: "Create cashback campaign", method: "POST",
    path: "/api/v1/admin/marketing/cashback-campaigns", confirm: "CREATE CASHBACK CAMPAIGN",
    description: "Create a draft fixed-paise or basis-point cashback campaign.",
    fields: [
      field("code", "Campaign code", "text", {required: true, minLength: 3, maxLength: 64}),
      field("name", "Name", "text", {required: true, maxLength: 120}),
      field("description", "Description", "textarea", {required: false, maxLength: 500}),
      selectField("reward_type", "Reward type", ["FIXED_PAISE", "PERCENT_BPS"]),
      paiseField("fixed_reward_paise", "Fixed reward", false),
      field("percentage_bps", "Percentage (basis points)", "integer", {required: false, min: 1, max: 10000}),
      paiseField("max_reward_paise", "Maximum reward", false),
      paiseField("minimum_order_paise", "Minimum order"),
      dateField("starts_at", "Starts at"), dateField("ends_at", "Ends at"),
    ],
  },
  lifecycle("cashback-campaign-activate", "Activate cashback campaign", "/api/v1/admin/marketing/cashback-campaigns/{campaign_id}/activate", () => idField("campaign_id", "Campaign ID"), "ACTIVATE CASHBACK CAMPAIGN"),
  marketingReview("cashback-campaign-disable", "Disable cashback campaign", "/api/v1/admin/marketing/cashback-campaigns/{campaign_id}/disable", "campaign_id", "Campaign ID", "DISABLE CASHBACK CAMPAIGN"),
  marketingReview("cashback-reward-void", "Void cashback reward", "/api/v1/admin/marketing/cashback-rewards/{reward_id}/void", "reward_id", "Reward ID", "VOID CASHBACK REWARD"),
  {
    id: "affiliate-create", group: "Marketing", label: "Create affiliate", method: "POST",
    path: "/api/v1/admin/marketing/affiliates", confirm: "CREATE AFFILIATE",
    description: "Create a pending affiliate and its commission policy.",
    fields: [
      field("code", "Affiliate code", "text", {required: true, minLength: 3, maxLength: 64}),
      field("display_name", "Display name", "text", {required: true, maxLength: 120}),
      field("owner_user_id", "Owner user ID", "uuid", {required: false}),
      selectField("commission_type", "Commission type", ["FIXED_PAISE", "PERCENT_BPS"]),
      paiseField("fixed_commission_paise", "Fixed commission", false),
      field("percentage_bps", "Percentage (basis points)", "integer", {required: false, min: 1, max: 10000}),
      paiseField("max_commission_paise", "Maximum commission", false),
      paiseField("minimum_order_paise", "Minimum order"),
    ],
  },
  lifecycle("affiliate-activate", "Activate affiliate", "/api/v1/admin/marketing/affiliates/{affiliate_id}/activate", () => idField("affiliate_id", "Affiliate ID"), "ACTIVATE AFFILIATE"),
  marketingReview("affiliate-suspend", "Suspend affiliate", "/api/v1/admin/marketing/affiliates/{affiliate_id}/suspend", "affiliate_id", "Affiliate ID", "SUSPEND AFFILIATE"),
  {
    id: "affiliate-conversion-create", group: "Marketing", label: "Attribute affiliate conversion", method: "POST",
    path: "/api/v1/admin/marketing/affiliate-conversions", confirm: "ATTRIBUTE CONVERSION", danger: true,
    description: "Attribute one settled order using external evidence; commissions remain pending review.",
    fields: [
      field("affiliate_id", "Affiliate ID", "uuid", {required: true}),
      field("order_id", "Settled order ID", "uuid", {required: true}), evidenceField(),
    ],
  },
  marketingReview("affiliate-conversion-void", "Void affiliate conversion", "/api/v1/admin/marketing/affiliate-conversions/{conversion_id}/void", "conversion_id", "Conversion ID", "VOID AFFILIATE CONVERSION"),
];

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function inputFor(definition) {
  let control;
  if (definition.type === "textarea" || definition.type === "json") {
    control = document.createElement("textarea");
    control.rows = definition.type === "json" ? 5 : 3;
  } else if (definition.type === "select") {
    control = document.createElement("select");
    if (!definition.required) control.append(new Option("Leave unchanged", ""));
    for (const option of definition.options) control.append(new Option(option.replaceAll("_", " "), option));
  } else {
    control = document.createElement("input");
    control.type = definition.type === "integer" ? "number" :
      definition.type === "datetime" ? "datetime-local" :
      definition.type === "checkbox" ? "checkbox" : definition.type === "password" ? "password" : "text";
  }
  control.name = definition.name;
  control.required = Boolean(definition.required);
  if (definition.placeholder) control.placeholder = definition.placeholder;
  if (definition.min !== undefined) control.min = String(definition.min);
  if (definition.max !== undefined) control.max = String(definition.max);
  if (definition.minLength !== undefined) control.minLength = definition.minLength;
  if (definition.maxLength !== undefined) control.maxLength = definition.maxLength;
  if (definition.pattern) control.pattern = definition.pattern;
  if (definition.autocomplete) control.autocomplete = definition.autocomplete;
  if (definition.type === "integer") control.step = "1";
  if (definition.type === "checkbox") control.checked = Boolean(definition.value);
  else if (definition.value !== undefined) control.value = String(definition.value);
  return control;
}

function fieldNode(definition) {
  const wrapper = el("div", "", definition.type === "checkbox" ? "action-field action-check" : "action-field");
  const label = el("label");
  const control = inputFor(definition);
  if (definition.type === "checkbox") label.append(control, el("span", definition.label));
  else label.append(el("span", `${definition.label}${definition.required ? " *" : ""}`), control);
  wrapper.append(label);
  if (definition.help) wrapper.append(el("p", definition.help, "field-help"));
  if (definition.allowNull) {
    const clearLabel = el("label", "", "null-control");
    const clearInput = document.createElement("input");
    clearInput.type = "checkbox";
    clearInput.name = `${definition.name}__null`;
    clearInput.addEventListener("change", () => {
      control.disabled = clearInput.checked;
      if (clearInput.checked) control.value = "";
    });
    clearLabel.append(clearInput, el("span", "Send null to clear this value"));
    wrapper.append(clearLabel);
  }
  return wrapper;
}

function valueFor(form, definition) {
  const control = form.elements.namedItem(definition.name);
  if (!(control instanceof HTMLElement)) throw new Error(`Missing field: ${definition.label}`);
  const nullControl = form.elements.namedItem(`${definition.name}__null`);
  if (nullControl instanceof HTMLInputElement && nullControl.checked) return null;
  if (definition.type === "checkbox") return control.checked;
  const raw = control.value.trim();
  if (!raw) {
    if (definition.required) throw new Error(`${definition.label} is required.`);
    return undefined;
  }
  if (definition.type === "uuid" && !UUID_PATTERN.test(raw)) throw new Error(`${definition.label} must be a valid UUID.`);
  if (definition.type === "integer") {
    if (!/^-?\d+$/.test(raw)) throw new Error(`${definition.label} must be a whole number.`);
    const value = Number(raw);
    if (!Number.isSafeInteger(value)) throw new Error(`${definition.label} is outside the browser's safe integer range.`);
    return value;
  }
  if (definition.type === "datetime") {
    const value = new Date(raw);
    if (Number.isNaN(value.getTime())) throw new Error(`${definition.label} is not a valid date and time.`);
    return value.toISOString();
  }
  if (definition.type === "json") {
    let value;
    try { value = JSON.parse(raw); } catch { throw new Error(`${definition.label} must be valid JSON.`); }
    if (definition.jsonKind === "array" && !Array.isArray(value)) throw new Error(`${definition.label} must be a JSON array.`);
    if (definition.jsonKind === "object" && (value === null || Array.isArray(value) || typeof value !== "object")) {
      throw new Error(`${definition.label} must be a JSON object.`);
    }
    return value;
  }
  return raw;
}

function requestParts(action, form) {
  let path = action.path;
  const body = {};
  for (const definition of action.fields) {
    const value = valueFor(form, definition);
    if (definition.source === "path") {
      if (value === undefined || value === null) throw new Error(`${definition.label} is required.`);
      path = path.replace(`{${definition.name}}`, encodeURIComponent(String(value)));
    } else if (value !== undefined) body[definition.name] = value;
  }
  if (/\{[^}]+\}/.test(path)) throw new Error("A required path identifier is missing.");
  return {path, body};
}

function responseSummary(response) {
  if (response === null || response === undefined) return "Operation completed with no response body.";
  return JSON.stringify(response, null, 2);
}

function actionCard(action) {
  const details = document.createElement("details");
  details.className = `action-card${action.danger ? " is-danger" : ""}`;
  const summary = document.createElement("summary");
  const title = el("span", "", "action-title");
  title.append(el("strong", action.label), el("small", `${action.method} ${action.path}`));
  summary.append(title, el("span", action.group, "ops-chip"));
  details.append(summary, el("p", action.description, "muted"));

  const form = document.createElement("form");
  form.className = "action-form";
  form.noValidate = false;
  const fieldGrid = el("div", "", "action-fields");
  for (const definition of action.fields) fieldGrid.append(fieldNode(definition));
  form.append(fieldGrid);

  const confirmation = field("confirmation", `Type ${action.confirm} to confirm`, "text", {
    required: true, autocomplete: "off",
  });
  const confirmNode = fieldNode(confirmation);
  confirmNode.classList.add("confirmation-field");
  form.append(confirmNode);

  const controls = el("div", "", "action-submit");
  const submit = el("button", action.label, action.danger ? "button danger" : "button");
  submit.type = "submit";
  controls.append(submit, el("p", "A fresh idempotency key is generated for this submission.", "muted small"));
  const output = el("pre", "", "action-output");
  output.tabIndex = 0;
  output.setAttribute("aria-label", "Server response");
  output.hidden = true;
  form.append(controls, output);

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const typed = form.elements.namedItem("confirmation")?.value.trim();
    if (typed !== action.confirm) {
      showStatus(`Type ${action.confirm} exactly before submitting.`, "error");
      return;
    }
    submit.disabled = true;
    submit.textContent = "Working…";
    output.hidden = true;
    showStatus("");
    try {
      const {path, body} = requestParts(action, form);
      const response = action.method === "PUT" ? await put(path, body, true) : await post(path, body, true);
      showStatus(`${action.label} completed. Review the server response below.`, "success");
      output.textContent = responseSummary(response);
      output.hidden = false;
      const confirmInput = form.elements.namedItem("confirmation");
      if (confirmInput) confirmInput.value = "";
    } catch (error) {
      const message = errorMessage(error);
      showStatus(message, "error");
      output.textContent = message;
      output.hidden = false;
    } finally {
      form.querySelectorAll('input[type="password"]').forEach(input => { input.value = ""; });
      submit.disabled = false;
      submit.textContent = action.label;
    }
  });
  details.append(form);
  return details;
}

export function renderAdminActions(chrome) {
  chrome(
    "Admin actions",
    "All server-supported mutations in one guarded workspace. Every submission is authenticated, idempotent and audited.",
    "/admin/actions",
  );
  const root = clear();
  const warning = el("section", "", "action-warning");
  warning.append(
    el("strong", "Verify before you mutate"),
    el("p", "IDs, bank evidence and lifecycle prerequisites are not inferred by the browser. Payment approval, dispute settlement and draw actions can change durable financial state."),
  );
  const toolbar = el("div", "", "ops-toolbar action-toolbar");
  const searchLabel = el("label");
  searchLabel.append(el("span", "Search actions"));
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "Series, payment, refund…";
  searchLabel.append(search);
  const groupLabel = el("label");
  groupLabel.append(el("span", "Group"));
  const groupSelect = document.createElement("select");
  groupSelect.append(new Option("All groups", ""));
  for (const group of [...new Set(ADMIN_ACTIONS.map(action => action.group))]) groupSelect.append(new Option(group, group));
  groupLabel.append(groupSelect);
  const count = el("p", "", "muted small action-count");
  toolbar.append(searchLabel, groupLabel, count);

  const list = el("div", "", "action-list");
  function update() {
    const term = search.value.trim().toLowerCase();
    const group = groupSelect.value;
    const visible = ADMIN_ACTIONS.filter(action =>
      (!group || action.group === group) &&
      (!term || `${action.label} ${action.description} ${action.path} ${action.group}`.toLowerCase().includes(term)));
    count.textContent = `${visible.length} of ${ADMIN_ACTIONS.length} actions`;
    list.replaceChildren(...visible.map(actionCard));
    if (!visible.length) list.append(el("p", "No admin action matches this filter.", "empty-state"));
  }
  search.addEventListener("input", update);
  groupSelect.addEventListener("change", update);
  root.append(warning, toolbar, list);
  update();
}
