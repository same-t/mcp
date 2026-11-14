# fetch-mcp

Web search + fetch MCP server (Go) over stdio.

## Tools
- `web_search(query, max_results)` — DuckDuckGo Lite search, returns title/url/snippet.
- `fetch_url(url, max_chars)` — fetch URL and return readable text.
- `fetch_urls(urls, max_chars)` — fetch multiple URLs.
- `search_and_fetch(query, max_results, max_chars_per_page)` — search then fetch top pages.

## Build

```bash
cd /root/fetch
go build -o fetch-mcp .
```
