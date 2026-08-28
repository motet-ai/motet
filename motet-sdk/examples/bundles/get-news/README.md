# get-news

Browser-assisted news aggregation workflow example.

## What It Demonstrates

- Multi-step workflow orchestration with dependencies
- Parallel command composition (`motet.apply`) for fan-out/fan-in fetch stages
- Browser-capable tool execution from commands
- Structured outputs for downstream consumption
- Graceful fallback when external fetches fail

## Commands

- `get-news.discover_sources`
- `get-news.fetch_source`
- `get-news.fetch_articles`
- `get-news.cluster_articles`
- `get-news.build_digest`

## Workflow

- `get-news.news_aggregation`
- `get-news.top_headlines` (no topic required; best for "get news")

## Typical Input Parameters

- `topic` (string): news topic, e.g. `"AI regulation"`
- `max_sources` (int): number of source URLs to fetch
- `fetch_tool_name` (string): browser-capable tool name (default `core.http_get_browser`)
- `max_chars` (int): max article content length to keep per source
- `min_overlap_terms` (int): keyword overlap threshold for clustering
- `max_items` (int): max digest items to output
