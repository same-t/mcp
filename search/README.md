# search-mcp

Web search + fetch MCP server (Python) over stdio.

## Tools
- `web_search(query, max_results)` — DuckDuckGo Lite search, returns title/url/snippet.
- `fetch_url(url, max_chars)` — fetch URL and return readable text.
- `fetch_urls(urls, max_chars)` — fetch multiple URLs in parallel.
- `search_and_fetch(query, max_results, max_chars_per_page)` — search then fetch top pages.

## Install

```bash
cd search
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Or run without installing:

```bash
cd search
PYTHONPATH=src python3 -m search_mcp.server
```

## Build (not required)

No build step; pure Python.

## OpenCode config

```json
{
  "mcp": {
    "search": {
      "type": "local",
      "command": ["python3", "-m", "search_mcp.server"],
      "environment": { "PYTHONPATH": "src" },
      "enabled": true
    }
  }
}
```