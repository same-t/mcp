package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"
)

func fetchHTTP(rawURL string) (string, string, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	req, err := http.NewRequest("GET", rawURL, nil)
	if err != nil {
		return "", "", err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	// Accept-Encoding omitted so Go auto-handles gzip/deflate decompression
	req.Header.Set("Cache-Control", "max-age=0")
	req.Header.Set("Sec-Ch-Ua", `"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"`)
	req.Header.Set("Sec-Ch-Ua-Mobile", "?0")
	req.Header.Set("Sec-Ch-Ua-Platform", `"Windows"`)
	req.Header.Set("Sec-Fetch-Dest", "document")
	req.Header.Set("Sec-Fetch-Mode", "navigate")
	req.Header.Set("Sec-Fetch-Site", "none")
	req.Header.Set("Sec-Fetch-User", "?1")
	req.Header.Set("Upgrade-Insecure-Requests", "1")
	resp, err := client.Do(req)
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 4*1024*1024))
	if err != nil {
		return "", "", err
	}
	ct := resp.Header.Get("Content-Type")
	return string(b), ct, nil
}

func stripHTML(html string) string {
	for _, tag := range []string{"script", "style", "noscript", "svg"} {
		re := regexp.MustCompile(`(?is)<` + tag + `[^>]*>.*?</` + tag + `>`)
		html = re.ReplaceAllString(html, " ")
	}
	html = regexp.MustCompile(`(?s)<[^>]+>`).ReplaceAllString(html, " ")
	html = htmlUnescape(html)
	html = regexp.MustCompile(`\s+`).ReplaceAllString(html, " ")
	return strings.TrimSpace(html)
}

func htmlUnescape(s string) string {
	return html.UnescapeString(s)
}

func extractTitle(html string) string {
	m := regexp.MustCompile(`(?is)<title[^>]*>(.*?)</title>`).FindStringSubmatch(html)
	if len(m) > 1 {
		return strings.TrimSpace(stripHTML(m[1]))
	}
	return ""
}

func decodeBingURL(u string) string {
	parsed, err := url.Parse(u)
	if err != nil {
		return u
	}
	v := parsed.Query().Get("u")
	if v == "" {
		return u
	}
	if strings.HasPrefix(v, "a1") {
		v = v[2:]
	}
	b, err := base64.RawURLEncoding.DecodeString(v)
	if err != nil {
		b, err = base64.StdEncoding.DecodeString(v)
		if err != nil {
			return u
		}
	}
	return string(b)
}

func searchWeb(query string, count int) ([]map[string]string, error) {
	if count <= 0 {
		count = 10
	}
	u := "https://lite.duckduckgo.com/lite/?q=" + url.QueryEscape(query)
	body, _, err := fetchHTTP(u)
	if err != nil {
		return nil, err
	}
	results := []map[string]string{}
	linkRe := regexp.MustCompile(`(?s)<a([^>]*)class='result-link'([^>]*)>(.*?)</a>`)
	snipRe := regexp.MustCompile(`(?s)<td[^>]*class='result-snippet'[^>]*>(.*?)</td>`)
	links := linkRe.FindAllStringSubmatch(body, -1)
	snips := snipRe.FindAllStringSubmatch(body, -1)
	for i, l := range links {
		if len(results) >= count {
			break
		}
		attrs := l[1] + l[2]
		hm := regexp.MustCompile(`href="([^"]+)"`).FindStringSubmatch(attrs)
		href := ""
		if len(hm) > 1 {
			href = hm[1]
		}
		// duckduckgo redirect
		if strings.Contains(href, "uddg=") {
			if parsed, err := url.Parse(href); err == nil {
				if v := parsed.Query().Get("uddg"); v != "" {
					href = v
				}
			}
		}
		title := stripHTML(l[3])
		snippet := ""
		if i < len(snips) {
			snippet = stripHTML(snips[i][1])
		}
		if title == "" {
			continue
		}
		results = append(results, map[string]string{
			"title":   title,
			"url":     href,
			"snippet": snippet,
		})
	}
	return results, nil
}

func jsonMarshal(v any) ([]byte, error) {
	return json.MarshalIndent(v, "", "  ")
}

var _ = os.Stdout
var _ = fmt.Sprintf
var _ = io.Discard