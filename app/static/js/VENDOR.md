# Vendored static assets

Third-party files served as-is from this repository (no CDN at run time, so the CSP stays
`script-src 'self'`). To update one: download the new exact version from the source below, replace
the file, and update this table in the same change.

| File | Package (npm) | Version | Source | Licence | sha256 |
|---|---|---|---|---|---|
| `js/htmx.min.js` | `htmx.org` | 2.0.11 | https://cdn.jsdelivr.net/npm/htmx.org@2.0.11/dist/htmx.min.js | 0BSD (Zero-Clause BSD) | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |
| `js/htmx-ext-sse.js` | `htmx-ext-sse` | 2.2.4 | https://cdn.jsdelivr.net/npm/htmx-ext-sse@2.2.4/dist/sse.min.js | 0BSD (Zero-Clause BSD) | `98a46496de0c3605fbffdce9167ba427bdd9553184f83f149c261891a92c0136` |
| `icons/sprite.svg` | `lucide-static` | 1.72.0 | https://cdn.jsdelivr.net/npm/lucide-static@1.72.0/icons/NAME.svg (see below) | ISC (+ MIT for icons derived from Feather), text in `icons/LICENSE` | `b0d138c64d3e32f89ddd27f2744a75df53c8cddb579eb698e0a807ba79c56479` |

`icons/LICENSE` is the `LICENSE` file of `lucide-static@1.72.0`.

## Lucide sprite

`icons/sprite.svg` is a small subset of Lucide, one `<symbol>` per icon, built from the individual
`icons/<name>.svg` files of `lucide-static@1.72.0` (the `<path>`/`<rect>` content is unchanged; the
shared stroke attributes moved onto each `<symbol>`). Icons: `calendar`, `check`, `chevron-down`,
`chevron-right`, `circle-alert`, `circle-check`, `clock`, `copy`, `download`, `external-link`,
`info`, `log-out`, `plus`, `refresh-cw`, `trash-2`, `triangle-alert`, `upload`, `user`, `x`.
Add an icon only when a screen needs it, and list it here.
