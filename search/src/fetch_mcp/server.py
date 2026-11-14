"""fetch-mcp server: web search + fetch (curl_cffi)."""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urljoin, urlparse, parse_qs, unquote

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fetch-mcp")

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


def _fetch(url: str, timeout: int = 30) -> tuple[str, str]:
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


@mcp.tool()
def web_search(query: str, max_results: int = 8) -> str:
    """Search the web via DuckDuckGo Lite. Returns JSON list of {title,url,snippet}."""
    url = "https://lite.duckduckgo.com/lite/?q=" + quote(query)
    html, _ = _fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    results = []
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
        results.append({"title": title, "url": href, "snippet": snippet})
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
def fetch_url(url: str, max_chars: int = 20000) -> str:
    """Fetch a URL and return readable text content."""
    html, ct = _fetch(url)
    title = _extract_title(html)
    text = _strip_html(html) if "html" in ct or "<html" in html.lower() else html
    if len(text) > max_chars:
        text = text[:max_chars] + "...[truncated]"
    out = ""
    if title:
        out += "TITLE: " + title + "\n\n"
    out += "URL: " + url + "\nCONTENT-TYPE: " + ct + "\n\n" + text
    return out


@mcp.tool()
def fetch_urls(urls: list[str], max_chars: int = 8000) -> str:
    """Fetch multiple URLs and return their text."""
    sb = []
    for u in urls:
        try:
            html, ct = _fetch(u)
            text = _strip_html(html) if "html" in ct or "<html" in html.lower() else html
            if len(text) > max_chars:
                text = text[:max_chars] + "...[truncated]"
            sb.append(f"=== {u} ===\n{text}\n")
        except Exception as e:
            sb.append(f"=== {u} ===\nERROR: {e}\n")
    return "\n".join(sb)


@mcp.tool()
def search_and_fetch(query: str, max_results: int = 3, max_chars_per_page: int = 6000) -> str:
    """Search the web then fetch details of top results."""
    url = "https://lite.duckduckgo.com/lite/?q=" + quote(query)
    html, _ = _fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    results = []
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
        results.append((title, href, snippet))

    sb = [f"QUERY: {query}\n"]
    for i, (title, href, snippet) in enumerate(results, 1):
        sb.append(f"### Result {i}: {title}\nURL: {href}\nSnippet: {snippet}\n")
        try:
            page_html, ct = _fetch(href)
            text = _strip_html(page_html) if "html" in ct or "<html" in page_html.lower() else page_html
            if len(text) > max_chars_per_page:
                text = text[:max_chars_per_page] + "...[truncated]"
            sb.append(f"CONTENT:\n{text}\n\n---\n")
        except Exception as e:
            sb.append(f"FETCH ERROR: {e}\n\n---\n")
    return "\n".join(sb)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
