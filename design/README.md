# E-Ticket interface concept

## Direction 02 — complete nine-screen concept

The current design-first handoff is in `v2/`:

- `site-architecture.md` — the proposed 22 main, 16 subpage and 6 information
  view inventory, route status, navigation and sitemap; review this before
  expanding the nine-screen board or changing application UI.
- `complete-ui-board.svg` — recommended Figma-importable vector board covering
  login, sign up, packages, user dashboard, ticket/scratch-card visual,
  payments, withdrawals, admin overview and admin functions.
- `complete-ui-board-preview.png` — rendered review image of that full board.
- `preview.html` — responsive, self-contained HTML/CSS preview. Its buttons are
  intentionally inactive and it makes no API calls.
- `page-details.md` — page-by-page layouts, states, backend criteria and
  implementation boundaries.
- `build_figma.js` — prepared Figma Plugin API source, **not executed**. The
  connected Figma write was stopped by an approval/usage limit. The native
  [E-Ticket Platform — Complete UI v2](https://www.figma.com/design/LSp6uJCfitMR3Elu715Pjk)
  file was created but contains no finished screens. Do not review that link as
  if it were the completed board.

Open `complete-ui-board.svg` locally to review the complete composition, or
drag it onto a Figma Design canvas for editable vector layers. SVG import does
not create reusable native components or working application behavior. The
earlier `v2/e-ticket-figma-board-v2.svg` and `.png` are retained as prior
exploration; `complete-ui-board.svg` is the fuller nine-screen handoff.

Design direction 02 was rendered and visually reviewed locally. No application
UI, payment workflow or provider integration was changed in this design step.

## Direction 01 — earlier four-screen concept

`e-ticket-figma-board.svg` is a vector design board for four representative
screens: desktop discovery, desktop buyer checkout, desktop payment operations,
and mobile buyer checkout. It is a design proposal, not an implemented screen
or a native `.fig` export.

To review it in Figma, open a Figma Design file and drag the SVG onto its canvas.
Figma imports SVG as editable vector layers. A native Figma file with reusable
components and text styles requires a connected Figma account; do not rename
the SVG to `.fig`.

The board uses illustrative amounts and labels only. No order, payment
destination, provider status or settlement is represented by its sample data.
The intended direction is a compact navy/coral/teal system with clear hierarchy,
large touch controls, responsive layouts and prominent verification notices.
Implementation should preserve all existing server-side payment and draw rules.

Design review is the next gate. Application UI code was not changed in this
design-first step.
