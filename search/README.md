# search-mcp

Web search + fetch MCP server (Python) over stdio.

## Tools
- `web_search(query, max_results, page, include_domains, exclude_domains)` — DuckDuckGo Lite search with pagination and domain filtering. Returns structured JSON.
- `fetch_url(url, max_chars)` — fetch URL and return readable text.
- `fetch_urls(urls, max_chars)` — fetch multiple URLs in parallel.
- `search_and_fetch(query, max_results, max_chars_per_page, include_domains, exclude_domains)` — search then fetch top pages.
- `search_parallel(queries, max_results_per_query, page, include_domains, exclude_domains)` — run multiple queries in parallel and deduplicate results.
- `deep_search(query, queries, max_results, fetch_top_n, max_chars_per_page, include_domains, exclude_domains)` — parallel multi-query search, deduplication, and content extraction for research workflows.

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