# Celestial row layout

Choose **Edit layout**, drag the grip beside a row (mouse, touch or pen), then
**Save layout**. Arrow buttons and keyboard Up/Down/Home/End provide the same
ordering. **Cancel** or Escape discards the draft. **Reset to default** selects
the original order; Save applies it.

Major dashboard sections and individual market panels can move across the page.
Table data rows retain their existing rankings and filters. Half-width market
cards retain their responsive widths. The original DOM nodes, IDs, event handlers,
input values, hidden states and polling functions are retained. No API writes,
trading flags, signals or broker behavior are changed. Authentication loss closes
the editor. Editing remains available while trading controls are disabled.

Preferences contain only known row IDs in localStorage, scoped to this browser
and site origin. They do not sync across devices and are removed when browser
site data is cleared. Unknown/duplicate saved IDs are ignored; newly available
panels append to the order. Unavailable storage leaves the layout usable for the
current visit and reports that persistence failed. No external JavaScript package
or additional polling is used.

The source page embeds `layout.css` and `layout.js` between explicit markers.
`patch.py INPUT --output NEW_FILE` can prepare a separate copy of the reviewed
composed AWS page. It refuses an existing output or duplicate insertion, and has
no service, broker, credential or installation operation. Do not replace the
installed page with a different branch's entire dashboard: preserve its overlays
and verify the prepared static HTML before any separate deployment.

The composed release artifact stays immutable. Tests inject the same layout
assets into that artifact in memory and intercept every request. CI covers both
source and composed pages, desktop/mouse and mobile/touch interaction, keyboard,
persistence, reset/cancel, sign-out, disabled trading controls, handler/input
preservation, late panels and storage failure. AWS deployment remains separate.

## Static webpage installation

From an exact reviewed checkout, run `sudo -n python3 -I -B
dashboard_layout/install.py` for a plan, or append `--apply` to install.
The target is fixed to `/var/www/html/dashboard.html`. Only the two reviewed
standard/composed predecessor pages and the exact installed overlay are accepted.
Unknown page content is preserved and blocks installation. CSS/JS are hash-pinned.

Apply holds the existing webpage-maintenance locks, writes a private original
HTML backup and receipt under `/var/backups/kadima-layout-*`, rechecks the current
page and assets, then atomically replaces the HTML while preserving ownership
and permissions. Readback verifies the exact installed bytes. Repeat application
is a no-op. Refresh the browser after success; served-page behavior still needs
the operator's browser check. There are no service restarts, API/broker requests,
database changes, trading flags or automatic activation. An unknown failure after
replacement is reported as unverified and keeps its original backup for review.
