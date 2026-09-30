import {clear, date, el, empty, get, link, money} from "./api.js";

function productCard(item, kind) {
  const isSeries = kind === "series";
  const remaining = isSeries
    ? Math.max(0, item.ticket_limit - item.sold_count - item.reserved_count)
    : item.inventory_limit === null ? null : Math.max(0, item.inventory_limit - item.sold_count - item.reserved_count);
  const available = (isSeries ? item.status === "OPEN" : item.is_active) && (remaining === null || remaining > 0);
  const node = el("article", "", "product-card");
  const top = el("div", "", "product-top");
  top.append(el("span", isSeries ? "S" : "P", "product-icon"));
  top.append(el("span", available ? "Available" : "Unavailable", `pill${available ? "" : " unavailable"}`));
  const detailHref = `/${isSeries ? "series" : "packages"}/${encodeURIComponent(item.id)}`;
  const title = el("h3");
  title.append(link(item.name, detailHref));
  node.append(top, title);
  if (item.description) node.append(el("p", item.description, "product-description"));

  const meta = el("div", "", "product-meta");
  if (isSeries) {
    meta.append(el("span", remaining === 1 ? "1 ticket left" : `${remaining} tickets left`));
    meta.append(el("span", `Sales end ${date(item.sales_end_at)}`));
    if (item.prizes?.length) meta.append(el("span", `${item.prizes.length} prize ${item.prizes.length === 1 ? "tier" : "tiers"}`));
  } else {
    const seriesCount = item.items.length;
    const ticketCount = item.items.reduce((sum, entry) => sum + entry.quantity, 0);
    meta.append(el("span", `${ticketCount} ${ticketCount === 1 ? "ticket" : "tickets"} across ${seriesCount} ${seriesCount === 1 ? "series" : "series"}`));
    if (remaining !== null) meta.append(el("span", `${remaining} packages left`));
  }
  node.append(meta);

  const bottom = el("div", "", "product-bottom");
  const price = el("div");
  price.append(el("p", "Listed price", "price-caption"), el("p", money(item.price_paise, item.currency), "metric"));
  bottom.append(price);
  if (available) {
    const href = `/checkout?type=${isSeries ? "SERIES" : "PACKAGE"}&id=${encodeURIComponent(item.id)}`;
    bottom.append(link("Review at checkout", href, "button"));
  }
  node.append(bottom, el("p", "Final amount and availability are confirmed at checkout.", "product-note"));
  node.append(link("View details", detailHref));
  return node;
}

async function products(kind) {
  const items = await get(`/api/v1/catalog/${kind}`);
  if (!items.length) return empty(kind === "series" ? "No public series are available yet." : "No packages are available yet.");
  const grid = el("div", "", "grid catalog-grid");
  for (const item of items) grid.append(productCard(item, kind === "series" ? "series" : "package"));
  clear().append(grid);
}

async function home() {
  const items = await get("/api/v1/catalog/series");
  const heading = el("div", "", "section-head");
  const title = el("div");
  title.append(el("p", "Start exploring", "eyebrow"), el("h2", "Featured series"));
  heading.append(title, link("View all series", "/series"));
  const content = clear();
  content.append(heading);
  if (!items.length) {
    content.append(el("p", "No series are available yet. Check back for new releases.", "empty-state"));
    return;
  }
  const grid = el("div", "", "grid catalog-grid");
  for (const item of items.slice(0, 3)) grid.append(productCard(item, "series"));
  content.append(grid);
}

async function results() {
  const items = await get("/api/v1/results");
  if (!items.length) return empty("No results have been published yet.");
  const list = el("div", "", "stack result-list");
  for (const result of items) {
    const node = el("article", "", "card result-card");
    const heading = el("div", "", "result-header");
    const title = el("div");
    title.append(el("h2", result.series_name), el("p", `Published ${date(result.published_at)} · ${result.eligible_ticket_count} eligible tickets`, "muted small"));
    heading.append(title, el("span", "Published", "pill"));
    node.append(heading);
    const proof = document.createElement("details");
    proof.append(el("summary", "View draw verification data"));
    for (const [label, value] of [
      ["Commitment", result.seed_commitment], ["Algorithm", result.algorithm_version],
      ["Revealed seed", result.seed_reveal], ["Candidate digest", result.eligible_tickets_digest],
      ["Result digest", result.result_digest],
    ]) proof.append(el("p", `${label}: ${value}`, "small"));
    node.append(proof);
    const winners = el("ul", "", "list");
    for (const winner of result.winners) {
      const row = el("li", "", "list-item");
      row.append(el("strong", `Rank ${winner.rank}: ${winner.title}`));
      row.append(el("p", `Ticket #${winner.ticket_serial_number} · ${money(winner.prize_paise)}`));
      winners.append(row);
    }
    node.append(winners);
    node.append(link("Full result and proof", `/results/${encodeURIComponent(result.series_id)}`, "button secondary"));
    list.append(node);
  }
  clear().append(list);
}

export async function render(page) {
  if (page === "home") return home();
  if (page === "series") return products("series");
  if (page === "packages") return products("packages");
  if (page === "results") return results();
}
