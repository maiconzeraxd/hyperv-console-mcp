# Vendored assets

Third-party files shipped inside the page, with the exact version they came
from. Nothing here is fetched at run time: `webui/page.py` inlines both files
into the HTML, so the window and `--serve` deliver one page that loads nothing
from the network.

| File | Package | Version | Licence | Source |
|---|---|---|---|---|
| `xterm.js` | [`xterm`](https://www.npmjs.com/package/xterm) | 5.3.0 | MIT | `https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js` |
| `xterm.css` | [`xterm`](https://www.npmjs.com/package/xterm) | 5.3.0 | MIT | `https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css` |

xterm.js is the terminal renderer the workspace uses for the KD, serial and
guest consoles: a real terminal widget rather than a styled `<pre>`, so the
output of a debugger or a shell looks and scrolls the way it does in a terminal.
Both files carry their own MIT licence header; the copyright notice is not
removed or altered.

## Updating

Download the two files for the new version, replace them here, update the table
above, and run the suite: `tests/test_webui_page.py` asserts the page carries
both files inline and that it loads nothing remote.
