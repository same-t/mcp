package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

func main() {
	s := server.NewMCPServer("fetch-mcp", "1.0.0", server.WithToolCapabilities(true))

	// web_search
	tool := mcp.NewTool("web_search",
		mcp.WithDescription("Search the web (DuckDuckGo) and return titles, urls, snippets."),
		mcp.WithString("query", mcp.Required()),
		mcp.WithNumber("max_results"),
	)
	s.AddTool(tool, func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		q := strArg(req, "query")
		n := intArg(req, "max_results")
		if n <= 0 {
			n = 8
		}
		res, err := searchWeb(q, n)
		if err != nil {
			return mcp.NewToolResultError(err.Error()), nil
		}
		b, _ := json.MarshalIndent(res, "", "  ")
		return mcp.NewToolResultText(string(b)), nil
	})

	// fetch_url
	tool = mcp.NewTool("fetch_url",
		mcp.WithDescription("Fetch a URL and return readable text content."),
		mcp.WithString("url", mcp.Required()),
		mcp.WithNumber("max_chars"),
	)
	s.AddTool(tool, func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		u := strArg(req, "url")
		maxc := intArg(req, "max_chars")
		if maxc <= 0 {
			maxc = 20000
		}
		body, ct, err := fetchHTTP(u)
		if err != nil {
			return mcp.NewToolResultError(err.Error()), nil
		}
		title := extractTitle(body)
		text := body
		if strings.Contains(ct, "html") || strings.Contains(body, "<html") {
			text = stripHTML(body)
		}
		if len(text) > maxc {
			text = text[:maxc] + "...[truncated]"
		}
		out := ""
		if title != "" {
			out += "TITLE: " + title + "\n\n"
		}
		out += "URL: " + u + "\nCONTENT-TYPE: " + ct + "\n\n" + text
		return mcp.NewToolResultText(out), nil
	})

	// fetch_urls
	tool = mcp.NewTool("fetch_urls",
		mcp.WithDescription("Fetch multiple URLs and return their text."),
		mcp.WithArray("urls", mcp.Required(), mcp.Items(map[string]any{"type": "string"})),
		mcp.WithNumber("max_chars"),
	)
	s.AddTool(tool, func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		urls := strSliceArg(req, "urls")
		maxc := intArg(req, "max_chars")
		if maxc <= 0 {
			maxc = 8000
		}
		var sb strings.Builder
		for _, u := range urls {
			body, ct, err := fetchHTTP(u)
			if err != nil {
				sb.WriteString("=== " + u + " ===\nERROR: " + err.Error() + "\n\n")
				continue
			}
			text := body
			if strings.Contains(ct, "html") || strings.Contains(body, "<html") {
				text = stripHTML(body)
			}
			if len(text) > maxc {
				text = text[:maxc] + "...[truncated]"
			}
			sb.WriteString("=== " + u + " ===\n" + text + "\n\n")
		}
		return mcp.NewToolResultText(sb.String()), nil
	})

	// search_and_fetch
	tool = mcp.NewTool("search_and_fetch",
		mcp.WithDescription("Search the web then fetch details of top results."),
		mcp.WithString("query", mcp.Required()),
		mcp.WithNumber("max_results"),
		mcp.WithNumber("max_chars_per_page"),
	)
	s.AddTool(tool, func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		q := strArg(req, "query")
		n := intArg(req, "max_results")
		if n <= 0 {
			n = 3
		}
		if n > 5 {
			n = 5
		}
		maxc := intArg(req, "max_chars_per_page")
		if maxc <= 0 {
			maxc = 6000
		}
		res, err := searchWeb(q, n)
		if err != nil {
			return mcp.NewToolResultError(err.Error()), nil
		}
		var sb strings.Builder
		sb.WriteString("QUERY: " + q + "\n\n")
		for i, r := range res {
			sb.WriteString(fmt.Sprintf("### Result %d: %s\nURL: %s\nSnippet: %s\n\n", i+1, r["title"], r["url"], r["snippet"]))
			body, ct, err := fetchHTTP(r["url"])
			if err != nil {
				sb.WriteString("FETCH ERROR: " + err.Error() + "\n\n")
				continue
			}
			text := body
			if strings.Contains(ct, "html") || strings.Contains(body, "<html") {
				text = stripHTML(body)
			}
			if len(text) > maxc {
				text = text[:maxc] + "...[truncated]"
			}
			sb.WriteString("CONTENT:\n" + text + "\n\n---\n\n")
		}
		return mcp.NewToolResultText(sb.String()), nil
	})

	if err := server.ServeStdio(s); err != nil {
		fmt.Fprintf(os.Stderr, "server error: %v\n", err)
		os.Exit(1)
	}
}

// helpers
func strArg(req mcp.CallToolRequest, name string) string {
	m, _ := req.Params.Arguments.(map[string]any)
	if v, ok := m[name]; ok && v != nil {
		switch t := v.(type) {
		case string:
			return t
		default:
			b, _ := json.Marshal(t)
			return string(b)
		}
	}
	return ""
}
func intArg(req mcp.CallToolRequest, name string) int {
	m, _ := req.Params.Arguments.(map[string]any)
	if v, ok := m[name]; ok && v != nil {
		switch t := v.(type) {
		case float64:
			return int(t)
		case int:
			return t
		case string:
			// parse
			n := 0
			fmt.Sscanf(t, "%d", &n)
			return n
		}
	}
	return 0
}
func strSliceArg(req mcp.CallToolRequest, name string) []string {
	m, _ := req.Params.Arguments.(map[string]any)
	v, ok := m[name]
	if !ok || v == nil {
		return nil
	}
	switch t := v.(type) {
	case []any:
		out := []string{}
		for _, e := range t {
			out = append(out, fmt.Sprintf("%v", e))
		}
		return out
	case []string:
		return t
	}
	return nil
}