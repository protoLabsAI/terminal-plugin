# Vendored xterm.js

Served offline from `/plugins/terminal/static/` (no CDN). Each file is the package's UMD
build (`lib/<name>.js`) copied verbatim. All MIT-licensed by the xterm.js authors.

| File | Package | Version |
|---|---|---|
| `xterm.js`, `xterm.css` | `@xterm/xterm` | 5.5.0 |
| `addon-fit.js` | `@xterm/addon-fit` | 0.10.0 |
| `addon-web-links.js` | `@xterm/addon-web-links` | 0.11.0 |
| `addon-canvas.js` | `@xterm/addon-canvas` | 0.7.0 |
| `addon-webgl.js` | `@xterm/addon-webgl` | 0.18.0 |
| `addon-search.js` | `@xterm/addon-search` | 0.15.0 |
| `addon-unicode11.js` | `@xterm/addon-unicode11` | 0.8.0 |

Staying on xterm 5.x deliberately: 6.0 drops the canvas renderer, which is the fallback
when WebGL is unavailable or loses its context. The addons above are the last releases
whose `peerDependencies` accept `@xterm/xterm ^5`.

To update: `npm pack @xterm/<pkg>@<version>`, copy `package/lib/<name>.js` here, update
this table.
