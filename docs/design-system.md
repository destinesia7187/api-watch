# API Watch visual system

The interface adapts the supplied `design/` reference and [Checkly](https://www.checklyhq.com/) visual direction to a monitoring workspace. API Watch retains its own name, pulse mark, application structure and product copy.

## Tokens and typography

`api_watch/static/design-tokens.css` contains ordinary CSS custom properties from `design/variables (1).css`. `style.css` maps those primitives to operational roles. The Tailwind `@theme` reference is retained as source material; no Tailwind build is required.

Inter is the primary interface typeface. Light display headings provide the visual emphasis on authentication screens; navigation, data labels and actions retain readable medium weights. JetBrains Mono is used for endpoints, HTTP methods, technical values, code and token displays. Both variable fonts are served locally with `font-display: swap`; system fallbacks remain available. See [font licenses](font-licenses.md).

## Color roles

| Role | Treatment |
| --- | --- |
| Page canvas | Void Navy `#041734` |
| Panels and nested surfaces | Orbit Slate `#0f172a` and Deck Charcoal `#1a1f36` |
| Main action | White fill with navy text |
| Brand, active navigation and latency plots | Signal Blue with lighter blue/cyan for small text and focus visibility |
| Supporting text | Readable Mist and lighter slate; the reference's dark Console Gray is not used for ordinary text on navy |
| Healthy checks and availability plots | Separate semantic green |
| Failure and open incidents | Separate semantic red |
| Degraded/recovering states and cautions | Semantic amber |
| Paused, skipped and unknown observations | Neutral styling with explicit labels |

Brand and health colors are separate variables. A blue navigation selection does not imply a successful check. A paused or unknown monitor with an open incident retains its incident marker. Empty observations remain gaps in charts.

## Structure and interaction

Spacing follows the supplied 4 px grid. Cards use 12 px corners, controls use 8 px corners, and status tags are fully rounded. Thin borders define surfaces. Horizontal top navigation keeps the product mark, primary destinations and account controls in the header, leaving the full workspace width for monitoring content. Overview totals appear as compact inline summary metrics above the checks. Technical tables, filtering, pagination, dialogs and keyboard behavior remain consistent across views.

Authentication, overview, monitor list/detail, incident history, settings and modal forms share the same tokens. On narrow screens, the menu button expands a navigation row below the main header. Summary metrics and controls wrap, and wide technical tables scroll horizontally. Keyboard focus remains visible, dialogs restore focus on dismissal, and reduced-motion preferences are respected.

The implementation uses local CSS, SVG and font assets. It does not load third-party scripts or fonts to render the dashboard. All visible interface copy remains English.
