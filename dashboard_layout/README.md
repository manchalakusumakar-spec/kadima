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
