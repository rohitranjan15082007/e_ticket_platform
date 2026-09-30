# E-Ticket UI direction 02 — page-by-page handoff

This is a **design proposal**, not a record of implemented functionality. The
companion `complete-ui-board.svg` contains nine illustrative desktop screens;
`preview.html` is a responsive, non-transactional HTML/CSS preview. Prices,
balances, ticket serials and names on these screens are sample content. The
server remains authoritative for identity, price, inventory, eligibility,
payment, ticket delivery and results.

Implementation update (2026-09-29): all 44 proposed product view routes now
serve v2-styled shells. The first fourteen were implemented as complete
data-driven view groups; the additional routes use shared public, account and
admin shells. This is route and visual coverage, not full feature readiness:
some admin editor/case workflows and owner-approved policy text remain absent.
Admin user identity is masked and the delivered-event notification view omits
payloads. Checkout still shows only configured
methods and relies on backend settlement; wallet and withdrawal values remain
server-authoritative. Illustrative board screens are design guidance, not
payment or deployment proof.

The complete page count and navigation hierarchy are fixed separately in
`site-architecture.md` (22 main + 16 subpages + 6 information pages). The nine
screens here are representative, not the full forty-four-view inventory.

## Visual language

- Tone: calm, premium and legible; warm ivory surfaces, deep-violet navigation,
  mint success accents, restrained amber/pink review notices. This is distinct
  from the earlier navy/coral board.
- Type: Inter (matching the current `base.css`), with system fallback. The
  8-pixel spacing rhythm, 12-column desktop content, 4-column mobile layout,
  12–20-pixel radii and clear 44-pixel-plus controls define the direction.
- Tokens: canvas `#F5F5F1`, ink `#20223B`, navigation `#292450`, primary
  `#5446A4`, mint `#D9F4C2`, muted text `#667085`, border `#E2E3EB`.
- Shared chrome: a small E-Ticket identity, public Explore/Packages/Results
  navigation, and signed-in sidebar or mobile bottom navigation. Admin uses a
  distinct Operations navigation. All financial status chips must be words as
  well as colors.
- At narrow widths, cards become one column, navigation collapses, sidebars
  become a menu/bottom navigation, and order summaries appear before the final
  action. Never hide safety notices or amounts on mobile.

## 01 — Login (`/login`)

Split-screen composition: the left violet pane introduces the service; the
right white pane has an email field, password field, primary **Sign in** action,
and a link to sign up. The mobile version stacks the brand pane above the form.
Show inline invalid-credentials and network errors without implying that sign-in
started a payment. Add visible keyboard focus and a busy state on submission.
The app now has a v2-styled login route; real authentication behavior still
depends on the configured backend and database.

## 02 — Sign up (`/register`)

Same visual family as login, with full name, email and password fields, a
minimum-12-character hint, **Create account** action, and existing-account
link. Show field-level validation, duplicate-email error, busy state and a
successful next-step message. Registration creates an account only—no ticket,
wallet credit, verified payment destination or withdrawal eligibility.

## 03 — Packages (`/packages`)

Header and filter row lead to a three-card product grid. Each card shows the
included ticket series/count, price from the server, inventory/availability,
and a details action. A selected package flows to checkout, where the server
rechecks inventory and calculates the final payable amount (including any
coupon). Show loading skeletons, sold-out/closed badges, no-results and
price-changed states. The pictured package titles and INR amounts are examples,
not catalog data or fixed prices.

## 04 — User dashboard (`/dashboard`)

Four summary cards—tickets, open orders, available wallet, held wallet—sit
above one clear next-action panel and recent order/ticket cards. The sidebar
links tickets, orders/checkout, payments, wallet, withdrawals and referrals.
Statuses must come from owner-scoped API responses. The card offers a path to
continue checkout, never a locally inferred `PAID` badge. Empty states should
invite browsing without showing fictitious balances or winnings.

## 05 — Ticket / scratch-card visual (`/tickets`, `/results`)

The decorative ticket surface displays series, allocated serial, order
reference and **Result pending** until the published draw is available. A
separate details panel links to public result proof: commitment, revealed seed,
candidate manifest and reproducible selection. No scratch gesture, visual
reveal or animation can decide or change an outcome. A true scratch-to-reveal
feature is **not implemented**; this is a card-style presentation of existing
ticket/result data. Show pre-delivery, pending-result, winner/non-winner and
result-unavailable states only from server responses.

## 06 — Payment section (`/checkout`; `/payment-demo` is preview-only)

Layout: step label **Choose → Review → Pay**, configured method list at left,
server-priced order summary at right, then a method-specific instruction/review
stage. Methods pictured: Manual UPI, exact-match P2P, Telegram Stars, and a
disabled white-label provider. A selected radio button or submitted UTR must
not show `PAID`; instead use **Instructions pending**, **Claim submitted / under
review**, **Awaiting confirmation**, **Reconciliation required** or server-
confirmed **Paid** as applicable. Keep recipient, exact amount, deadline and
frozen transaction ID visible only once an eligible P2P match is assigned.

Current limitations must remain visible in a live implementation: Manual UPI
may be disabled by configuration, P2P requires a qualifying withdrawal and
independent evidence, Telegram invoice dispatch needs a separately operated
bot, and white-label checkout initiation is disabled until a provider adapter
exists. A screenshot, UTR or receiver statement is a claim, never settlement.
No preview control is wired to payment APIs.

## 07 — Withdrawals (`/withdrawals`, `/wallet`)

Top cards show available and held balances. An eligible maximum is shown only
when a safe server-calculated preview exists; the current UI explicitly says
it is checked on submit. The
form requests an amount in rupees and displays the already operations-verified
destination; it does not enroll or verify a new destination. The side panel
shows request history/timeline and the meaning of a hold. V1 defaults to 50%
of the **pre-hold eligible** available balance, bounded by configured limits;
one unresolved request per user, exact-amount matching and no split matching.
If payment details have been exposed, expiry or missing proof cannot trigger
automatic hold release. Show ineligible, no verified destination, pending
match, exposed, claimed, disputed, reconciliation and completed states.

## 08 — Admin overview (`/admin`)

Operations sidebar plus server-derived cards for open series, pending orders,
manual proofs and disputes. Three review areas—catalog, payment review and
finance—make the next safe action clear. Counts are triage signals, **not**
payment verification. Use a role-gated entry, bounded lists and redacted
metadata. Empty and error states should never show invented zeroes if the API
failed. Existing admin web pages are primarily read-only; this design does not
claim that every depicted action is already exposed in the UI.

## 09 — Admin functions and criteria (`/admin/*`)

The board's eight tiles are navigation/ownership concepts, not one-click
approval buttons:

| Section | Intended content and required gate |
|---|---|
| Users / access | Role-gated user lists, status, redacted audit reads. |
| Series / packages | Lifecycle, finite inventory, package composition and server price snapshots. |
| Draw / results | Commitment before sales, closure, seed reveal, proof review and publication. |
| Payments | Manual claim queue, signed provider events, immutable evidence and accountable decision. |
| Withdrawals / P2P | Eligibility snapshots, holds, exact FIFO match, exposure and safe reconciliation. |
| Disputes / refunds | Independent evidence, reason, audit trail; a refund case is not an executed payout. |
| Revenue | Balanced integer-paise allocations, journal links and pending reward records. |
| Marketing / audit | Coupon/policy state, reviewed rewards and bounded/redacted audit metadata. |

Any mutating admin action needs server-side role checks, state-transition
validation, an audit reason and evidence appropriate to risk. No UI shortcut
can bypass the financial service or directly write a `PAID` state.

### How each admin page should look

All pages share the Operations sidebar, title/role context, a filter bar, a
bounded table and a right-side detail panel on desktop. On mobile the detail
panel becomes a full-screen view with a visible back action. A row click opens
detail; it does not mutate state. Each table has loading, empty, error and
permission-denied states, stable IDs, timestamps and status text.

| Page | Main canvas | Detail and decision area |
|---|---|---|
| Users | Search by safe ID/email fragment, status and role; table shows redacted identity, role and created date. | Account timeline and bounded audit metadata. Role changes, if offered later, require a separate permissioned and audited flow. |
| Series | Lifecycle/status tabs, inventory totals, sale window and result commitment. | Series detail, immutable commitment and warnings before closure; draw actions only when backend prerequisites pass. |
| Packages | Active/closed filters, package name, included series, server price and remaining inventory. | Package composition and immutable order-price snapshot; unavailable catalog cannot be selected for new orders. |
| Draw/results | Commitment → sales close → candidate freeze → reveal → publication timeline. | Seed-digest/proof links, candidate count and winner ranks; never an arbitrary winner-picker control. |
| Payments | Separate Manual UPI, P2P, Telegram and provider-event queues with method, amount, age and review status. | Evidence metadata and server events side by side. Approval controls, if implemented, require independent evidence and reason—not UTR-only confirmation. |
| Withdrawals/P2P | Queue by held, unmatched, exposed, claimed, disputed and reconciliation states. | Eligibility snapshot, exact match, frozen destination, deadline and hold history. Exposed attempts have no quick-release button. |
| Disputes/refunds | Risk/status filters and case age, linked match/order/refund IDs. | Evidence chronology, accountable resolution reason and separate payout status; creating a refund case must not appear as sending money. |
| Revenue | Date/series filters, bucket totals and integer-paise reconciliation indicators. | Journal links and export scope; a discrepancy is a visible error, not rounded away. |
| Marketing | Coupon/policy list, pending referrals/cashback/affiliate rewards. | Eligibility evidence and review/void reason. Pending reward is not wallet credit. |
| Audit | Date/actor/action filters and bounded newest-first metadata list. | Redacted event identifiers and safe metadata only; no secret payload or unrestricted state snapshots. |

These layouts are handoff specifications. Existing web admin pages are
primarily read-only, and some operational decisions currently live in guarded
APIs; this document does not claim the proposed control surfaces are built.

## Shared state and implementation checklist

Every network-backed screen needs loading, empty, validation, failure,
permission-denied and stale-data states. Focus styles, labels, keyboard order,
contrast and 44-pixel touch targets are part of implementation QA. API-driven
amounts are formatted from integer paise; no client-side float is an
authoritative balance. Keep a visible design/sample badge in previews.

Before app code changes, confirm this visual direction. Then implement in
small route groups (auth/public catalog; user dashboard/tickets; checkout and
withdrawals; read-only admin), checking each against actual API contracts and
responsive/accessibility behavior. Figma SVG import preserves vector artwork,
but it does not by itself create reusable native Figma components or a working
application.
