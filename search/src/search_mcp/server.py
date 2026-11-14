"""search-mcp server: web search + fetch (curl_cffi) over stdio."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urljoin, urlparse, parse_qs, unquote

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("search-mcp")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return max(lo, min(n, hi))


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r} (only http/https allowed)")
    if not parsed.netloc:
        raise ValueError(f"Invalid URL (missing host): {url!r}")


def _fetch(url: str, timeout: int = 30) -> tuple[str, str]:
    _validate_url(url)
    r = cffi_requests.get(url, headers=DEFAULT_HEADERS, impersonate="chrome124", timeout=timeout)
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    return r.text, ct


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def _decode_ddg_url(href: str) -> str:
    if "uddg=" in href:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def _search_ddg(query: str, max_results: int) -> list[tuple[str, str, str]]:
    url = "https://lite.duckduckgo.com/lite/?q=" + quote(query)
    html, _ = _fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    results: list[tuple[str, str, str]] = []
    for a in soup.find_all("a", class_="result-link"):
        if len(results) >= max_results:
            break
        href = _decode_ddg_url(a.get("href", ""))
        title = a.get_text(strip=True)
        snippet = ""
        parent = a.find_parent("tr")
        if parent:
            nxt = parent.find_next_sibling("tr")
            if nxt:
                td = nxt.find("td", class_="result-snippet")
                if td:
                    snippet = td.get_text(" ", strip=True)
        if not title:
            continue
        results.append((title, href, snippet))
    return results


@mcp.tool()
def web_search(query: str, max_results: int = 8) -> str:
    """Search the web via DuckDuckGo Lite. Returns JSON list of {title,url,snippet}."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    max_results = _clamp_int(max_results, 8, 1, 20)
    results = _search_ddg(query.strip(), max_results)
    return json.dumps(
        [{"title": t, "url": u, "snippet": s} for t, u, s in results],
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def fetch_url(url: str, max_chars: int = 20000) -> str:
    """Fetch a URL and return readable text content."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    url = url.strip()
    max_chars = _clamp_int(max_chars, 20000, 100, 200000)
    html, ct = _fetch(url)
    title = _extract_title(html)
    if "html" in ct.lower() or "<html" in html.lower():
        text = _strip_html(html)
    else:
        text = html
    if len(text) > max_chars:
        text = text[:max_chars] + "...[truncated]"
    out = ""
    if title:
        out += "TITLE: " + title + "\n\n"
    out += "URL: " + url + "\nCONTENT-TYPE: " + ct + "\n\n" + text
    return out


def _fetch_one(url: str, max_chars: int) -> str:
    try:
        html, ct = _fetch(url)
        if "html" in ct.lower() or "<html" in html.lower():
            text = _strip_html(html)
        else:
            text = html
        if len(text) > max_chars:
            text = text[:max_chars] + "...[truncated]"
        return f"=== {url} ===\n{text}\n"
    except Exception as e:  # noqa: BLE001 - report per-URL errors without aborting batch
        return f"=== {url} ===\nERROR: {e}\n"


@mcp.tool()
def fetch_urls(urls: list[str], max_chars: int = 8000) -> str:
    """Fetch multiple URLs and return their text."""
    if not isinstance(urls, list) or not urls:
        raise ValueError("urls must be a non-empty list of strings")
    cleaned = [str(u).strip() for u in urls if str(u).strip()]
    if not cleaned:
        raise ValueError("urls must contain at least one non-empty string")
    max_chars = _clamp_int(max_chars, 8000, 100, 200000)
    workers = min(len(cleaned), 8)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_fetch_one, u, max_chars) for u in cleaned]
        results = [f.result() for f in futures]
    return "\n".join(results)


@mcp.tool()
def search_and_fetch(query: str, max_results: int = 3, max_chars_per_page: int = 6000) -> str:
    """Search the web then fetch details of top results."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    max_results = _clamp_int(max_results, 3, 1, 5)
    max_chars_per_page = _clamp_int(max_chars_per_page, 6000, 100, 200000)

    results = _search_ddg(query.strip(), max_results)

    lines = [f"QUERY: {query.strip()}\n"]
    # Fetch pages in parallel while preserving result order.
    urls = [r[1] for r in results]
    with ThreadPoolExecutor(max_workers=min(len(urls), 5) or 1) as ex:
        futures = [ex.submit(_fetch_one, u, max_chars_per_page) for u in urls]
        fetched = [f.result() for f in futures]

    for i, (title, href, snippet) in enumerate(results, 1):
        lines.append(f"### Result {i}: {title}\nURL: {href}\nSnippet: {snippet}\n")
        # _fetch_one includes its own "=== url ===" header; strip it for readability.
        body = fetched[i - 1]
        marker = f"=== {href} ===\n"
        if body.startswith(marker):
            body = body[len(marker):]
        if body.endswith("\n"):
            body = body[:-1]
        lines.append(f"CONTENT:\n{body}\n\n---\n")
    return "\n".join(lines)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
