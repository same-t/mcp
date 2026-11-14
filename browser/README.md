# browser-mcp

Browser automation MCP server extracted from `samet-agent`. Exposes the full
browser API over MCP stdio, backed by SeleniumBase UC mode (undetected Chrome)
with CDP network capture.

Compatible with both **OpenCode** and **Claude Code** via the bundled config
files (`.mcp.json` and `opencode.json`).

## Install

```bash
cd browser
pip install -e .            # core (mcp)
pip install "seleniumbase>=4.40"   # browser backend (optional but required for tools)
```

Chrome must be installed on the machine (UC mode uses real Chrome).

## Run standalone

```bash
cd browser
PYTHONPATH=src python -m browser_mcp.server
```

## Configure

- Claude Code: copy/link `browser/.mcp.json` to your project root (or merge
  the `mcpServers.browser` entry into your existing `.mcp.json`).
- OpenCode: merge the `mcp.browser` entry from `browser/opencode.json` into
  your `opencode.json`.

Environment variables:

- `SAMET_WORKSPACE` — working directory used for screenshots/cookies/network
  logs (default: current directory).
- `SAMET_BROWSER_HEADLESS` — force headless/headed mode when set.

## Tools

Navigation, element observation, clicking/typing, forms, tabs, frames, dialogs,
cookies/storage, network capture, screenshots, JS evaluation, accessibility
tree, and Ghostwire (JS tracing) helpers. Always call `browser_elements` first
before clicking/typing.
