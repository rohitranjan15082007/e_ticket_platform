import {clear, el, errorMessage, get, link, showStatus, signOut, token} from "./api.js";

const area = document.body.dataset.area;
const page = document.body.dataset.page;
const nav = document.getElementById("site-nav");

async function navigation() {
  if (area === "public") {
    if (!nav.querySelector('a[href="/how-it-works"]')) nav.append(link("How it works", "/how-it-works"));
    const footer = document.querySelector(".site-footer .shell");
    if (footer && !footer.querySelector('a[href="/terms"]')) {
      const info = el("nav", "", "info-links");
      info.setAttribute("aria-label", "Information pages");
      for (const [label, href] of [["Payment methods", "/payment-methods"], ["Terms", "/terms"],
        ["Privacy", "/privacy"], ["Refund policy", "/refund-policy"],
        ["Responsible use", "/responsible-use"]]) info.append(link(label, href));
      footer.append(info);
    }
  }
  if (!token()) {
    nav.append(link("Sign in", "/login"), link("Create account", "/register", "nav-cta"));
    return;
  }
  try {
    const user = await get("/api/v1/auth/me", true);
    nav.append(link("Dashboard", "/dashboard"), link("Tickets", "/tickets"), link("Orders", "/orders"), link("Wallet", "/wallet"));
    if (user.roles.includes("admin")) nav.append(link("Admin", "/admin"));
    const out = el("button", "Sign out", "nav-button");
    out.type = "button";
    out.addEventListener("click", () => { signOut(); location.assign("/"); });
    nav.append(out);
  } catch {
    nav.append(link("Sign in", "/login"));
  }
}

async function main() {
  await navigation();
  const modules = {
    public: () => import(page === "payment-demo" ? "./payment_demo.js" :
      ["login", "register"].includes(page) ? "./auth.js" :
      page === "content" ? "./content_v2.js" : "./catalog.js"),
    user: () => import(page === "checkout" ? "./checkout.js" :
      ["wallet", "withdrawals"].includes(page) ? "./wallet.js" :
      ["dashboard", "tickets", "orders", "order-detail", "payments"].includes(page) ? "./account_v2.js" :
      page === "utility" ? "./utility_v2.js" : "./user.js"),
    admin: () => import("./admin.js"),
  };
  const module = await modules[area]();
  await module.render(page);
}
main().catch(error => {
  clear().append(el("p", "This page could not load. Please retry.", "empty-state"));
  showStatus(errorMessage(error), "error");
});
