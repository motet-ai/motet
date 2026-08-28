/**
 * Motet - Ops Dashboard - Developer Onboarding Docs Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Renders developer onboarding markdown from the API with a grouped
 *     left-rail nav (search, then Start, Concepts, Build, Runtime, State,
 *     Operate, Surfaces, Guides) and a main content area. Search hits open
 *     in a collapsible right pane so titles and snippets are readable.
 *     A new query (or Enter) reopens a collapsed results pane. Supports
 *     Mermaid diagrams.
 *
 * Dependencies:
 *     - antd Layout/Typography/Collapse for page chrome and grouped doc list
 *     - @tanstack/react-query for doc list/content fetching
 *     - MarkdownWithMermaid for markdown + Mermaid rendering
 *     - ThemeContext for dark-mode-aware Mermaid/docs rendering
 *
 * Usage:
 *     <Route path="/developer-docs/:docId" element={<DeveloperDocsPage />} />
 *
 * Notes:
 *     Left-rail doc links are plain divs (not Menu items), so they must set
 *     theme token text colors explicitly — otherwise dark mode inherits an
 *     invisible/near-invisible color against the transparent sider.
 *     Start is open by default; navigating to a page opens its section
 *     without closing sections the reader already expanded.
 *     Documentation home is ``00-landing-page``. The rail search box sits
 *     above the "Documentation" heading (product version beside the label),
 *     which navigates home. ``README``
 *     aliases to home so in-doc "Documentation Home" links do not fall
 *     through to Quick Start.
 *     Navigation keeps the previous page visible (placeholderData), prefetches
 *     on hover, and intercepts in-doc ``.md`` links so the URL does not hop
 *     through a spinner state.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Layout, Spin, Alert, Typography, theme, Collapse, ConfigProvider, Input, Button, Tooltip } from "antd";
import { MenuFoldOutlined, MenuUnfoldOutlined } from "@ant-design/icons";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { MarkdownWithMermaid } from "../components/MarkdownWithMermaid";
import { useTheme } from "../context/ThemeContext";

const { Sider, Content } = Layout;
const { Title } = Typography;

interface DocItem {
  id: string;
  filename: string;
  title: string;
  section?: string;
}

interface DocSection {
  id: string;
  title: string;
  items: DocItem[];
}

interface DocList {
  version?: string;
  items: DocItem[];
  sections: DocSection[];
}

interface SearchHit {
  id: string;
  title: string;
  section: string;
  section_title: string;
  snippet: string;
  heading?: string | null;
  score: number;
}

interface SearchResult {
  query: string;
  items: SearchHit[];
}

async function fetchDocList(): Promise<DocList> {
  const r = await fetch("/api/v1/developer-docs");
  if (!r.ok) throw new Error(r.statusText);
  const data = await r.json();
  const items: DocItem[] = data.items ?? [];
  const sections: DocSection[] = data.sections?.length
    ? data.sections
    : items.length
      ? [{ id: "docs", title: "Docs", items }]
      : [];
  return { version: data.version, items, sections };
}

async function fetchDocContent(docId: string): Promise<string> {
  const r = await fetch(`/api/v1/developer-docs/${docId}`);
  if (!r.ok) throw new Error(r.statusText);
  return r.text();
}

async function fetchDocSearch(query: string): Promise<SearchResult> {
  const r = await fetch(`/api/v1/developer-docs/search?q=${encodeURIComponent(query)}`);
  if (!r.ok) throw new Error(r.statusText);
  return r.json();
}

const DOCS_HOME_ID = "00-landing-page";
const DOC_STALE_MS = 5 * 60 * 1000;
const DOC_ID_PATTERN = /^\d{2}[a-z]?-[a-z0-9-]+$/;

/** Normalize doc id from URL: strip trailing .md so links like "09-mcp-integration.md" work. */
function normalizeDocId(docId: string | undefined): string | undefined {
  if (!docId) return undefined;
  const stripped = docId.endsWith(".md") ? docId.slice(0, -3) : docId;
  if (stripped.toLowerCase() === "readme") return DOCS_HOME_ID;
  return stripped;
}

function docIdFromHref(href: string | null, currentUrl: string): string | undefined {
  if (!href || href.startsWith("#") || href.startsWith("mailto:")) return undefined;
  let pathname: string;
  try {
    pathname = new URL(href, currentUrl).pathname;
  } catch {
    return undefined;
  }
  const last = pathname.split("/").pop();
  const id = normalizeDocId(last);
  if (!id) return undefined;
  if (id === DOCS_HOME_ID || DOC_ID_PATTERN.test(id)) return id;
  return undefined;
}

export function DeveloperDocsPage() {
  const { docId: paramDocId } = useParams<{ docId?: string }>();
  const normalizedParamId = normalizeDocId(paramDocId);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const darkMode = useTheme();
  const { token } = theme.useToken();
  const initialRedirectDone = useRef(false);
  const contentPaneRef = useRef<HTMLDivElement>(null);
  const [openSections, setOpenSections] = useState<string[]>(["start"]);
  const [searchInput, setSearchInput] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [resultsCollapsed, setResultsCollapsed] = useState(false);

  useEffect(() => {
    const handle = window.setTimeout(() => setDebouncedQuery(searchInput.trim()), 200);
    return () => window.clearTimeout(handle);
  }, [searchInput]);

  const { data: listData, isLoading: listLoading, error: listError } = useQuery({
    queryKey: ["developer-docs-list"],
    queryFn: fetchDocList,
    staleTime: DOC_STALE_MS,
  });

  const prefetchDoc = useCallback((docId: string) => {
    void queryClient.prefetchQuery({
      queryKey: ["developer-docs-content", docId],
      queryFn: () => fetchDocContent(docId),
      staleTime: DOC_STALE_MS,
    });
  }, [queryClient]);

  const list = listData?.items ?? [];
  const sections = listData?.sections ?? [];
  const homeId = list.some((d) => d.id === DOCS_HOME_ID) ? DOCS_HOME_ID : list[0]?.id ?? null;
  const paramIdValid = normalizedParamId && list.some((d) => d.id === normalizedParamId);
  const currentId = paramIdValid ? normalizedParamId! : homeId;

  const currentSectionId = useMemo(
    () => sections.find((section) => section.items.some((item) => item.id === currentId))?.id,
    [sections, currentId],
  );
  const railSections = useMemo(
    () => sections.filter((section) => section.id !== "home"),
    [sections],
  );
  const onHome = currentId === homeId;
  const searchActive = debouncedQuery.length >= 2;

  const { data: searchData, isFetching: searchLoading } = useQuery({
    queryKey: ["developer-docs-search", debouncedQuery],
    queryFn: () => fetchDocSearch(debouncedQuery),
    enabled: searchActive,
    staleTime: DOC_STALE_MS,
    placeholderData: keepPreviousData,
  });
  const searchHits = searchData?.items ?? [];
  const resultsLabel =
    searchLoading && searchHits.length === 0
      ? "Searching"
      : searchHits.length === 1
        ? "1 match"
        : `${searchHits.length} matches`;

  useEffect(() => {
    if (debouncedQuery.length >= 2) setResultsCollapsed(false);
  }, [debouncedQuery]);

  useEffect(() => {
    if (!currentSectionId || currentSectionId === "home") return;
    setOpenSections((prev) =>
      prev.includes(currentSectionId) ? prev : [...prev, currentSectionId],
    );
  }, [currentSectionId]);

  // Reset redirect guard when URL has a valid doc (so we can redirect again if user goes to /developer-docs with no doc)
  if (paramIdValid) initialRedirectDone.current = false;

  // Redirect to canonical URL without .md when user lands with .md (e.g. from in-doc links)
  useEffect(() => {
    if (paramDocId?.endsWith(".md") && normalizedParamId && list.some((d) => d.id === normalizedParamId)) {
      navigate(`/developer-docs/${normalizedParamId}`, { replace: true });
    }
  }, [paramDocId, normalizedParamId, list, navigate]);

  // Single redirect to first doc when URL has no doc or invalid doc (ref prevents navigation loop)
  useEffect(() => {
    if (list.length === 0 || !homeId || initialRedirectDone.current) return;
    if (normalizedParamId && list.some((d) => d.id === normalizedParamId)) return;
    initialRedirectDone.current = true;
    navigate(`/developer-docs/${homeId}`, { replace: true });
  }, [list.length, homeId, normalizedParamId, list, navigate]);

  const {
    data: content,
    isLoading: contentLoading,
    isPlaceholderData,
    error: contentError,
  } = useQuery({
    queryKey: ["developer-docs-content", currentId],
    queryFn: () => fetchDocContent(currentId!),
    enabled: !!currentId,
    staleTime: DOC_STALE_MS,
    placeholderData: keepPreviousData,
  });

  useEffect(() => {
    for (const section of sections) {
      if (!openSections.includes(section.id)) continue;
      for (const item of section.items) {
        prefetchDoc(item.id);
      }
    }
  }, [openSections, sections, prefetchDoc]);

  useEffect(() => {
    if (isPlaceholderData || !content) return;
    contentPaneRef.current?.scrollTo({ top: 0 });
  }, [currentId, isPlaceholderData, content]);

  const onContentClick = (event: MouseEvent<HTMLDivElement>) => {
    const anchor = (event.target as HTMLElement).closest("a");
    if (!anchor || anchor.target === "_blank" || anchor.hasAttribute("download")) return;
    const id = docIdFromHref(anchor.getAttribute("href") || anchor.href, window.location.href);
    if (!id || !list.some((item) => item.id === id)) return;
    event.preventDefault();
    prefetchDoc(id);
    navigate(`/developer-docs/${id}`);
  };

  if (listError) {
    return (
      <Alert
        type="error"
        title="Failed to load doc list"
        description={String(listError)}
        showIcon
      />
    );
  }

  return (
    <Layout style={{ background: "transparent", minHeight: 400 }}>
      <Sider
        width={280}
        style={{
          background: "transparent",
          borderRight: `1px solid ${token.colorBorder}`,
          paddingTop: 8,
          color: token.colorText,
        }}
      >
        <Input.Search
          allowClear
          size="small"
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
          onSearch={(value) => {
            const query = value.trim();
            setSearchInput(query);
            setDebouncedQuery(query);
            if (query.length >= 2) setResultsCollapsed(false);
          }}
          placeholder="Search Documentation"
          style={{ margin: "0 8px 8px", width: "calc(100% - 16px)" }}
        />
        <div
          style={{
            display: "flex",
            alignItems: "baseline",
            justifyContent: "space-between",
            gap: 8,
            margin: "0 8px 8px",
            padding: "6px 12px",
            borderRadius: 4,
            background: onHome ? token.colorPrimaryBg : undefined,
          }}
        >
          <Title
            level={5}
            role="link"
            tabIndex={0}
            style={{
              margin: 0,
              color: onHome ? token.colorPrimaryText : token.colorText,
              cursor: "pointer",
            }}
            onMouseEnter={() => prefetchDoc(homeId ?? DOCS_HOME_ID)}
            onClick={() => navigate(`/developer-docs/${homeId ?? DOCS_HOME_ID}`)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                navigate(`/developer-docs/${homeId ?? DOCS_HOME_ID}`);
              }
            }}
          >
            Documentation
          </Title>
          {listData?.version ? (
            <Typography.Text
              type="secondary"
              title={`Motet ${listData.version}`}
              style={{
                fontSize: 12,
                fontWeight: 500,
                fontVariantNumeric: "tabular-nums",
                flexShrink: 0,
              }}
            >
              {listData.version}
            </Typography.Text>
          ) : null}
        </div>
        {listLoading ? (
          <div style={{ padding: 16, textAlign: "center" }}>
            <Spin size="small" />
          </div>
        ) : (
          <div style={{ maxHeight: "calc(100vh - 200px)", overflowY: "auto" }}>
            <ConfigProvider theme={{ token: { motion: false } }}>
              <Collapse
                ghost
                size="small"
                activeKey={openSections}
                onChange={(keys) => setOpenSections(Array.isArray(keys) ? keys : [keys])}
                items={railSections.map((section) => ({
                  key: section.id,
                  label: (
                    <span
                      style={{
                        color: token.colorTextSecondary,
                        fontSize: 12,
                        fontWeight: 600,
                        letterSpacing: 0.2,
                      }}
                    >
                      {section.title}
                    </span>
                  ),
                  children: (
                    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                      {section.items.map((item) => {
                        const selected = item.id === currentId;
                        return (
                          <div
                            key={item.id}
                            role="button"
                            tabIndex={0}
                            style={{
                              cursor: "pointer",
                              background: selected ? token.colorPrimaryBg : undefined,
                              color: selected ? token.colorPrimaryText : token.colorText,
                              borderRadius: 4,
                              padding: "6px 12px",
                              fontSize: 13,
                            }}
                            onMouseEnter={() => prefetchDoc(item.id)}
                            onFocus={() => prefetchDoc(item.id)}
                            onClick={() => navigate(`/developer-docs/${item.id}`)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                navigate(`/developer-docs/${item.id}`);
                              }
                            }}
                          >
                            {item.title}
                          </div>
                        );
                      })}
                    </div>
                  ),
                }))}
              />
            </ConfigProvider>
          </div>
        )}
      </Sider>
      <Content
        ref={contentPaneRef}
        style={{ padding: "0 24px 24px", overflow: "auto", position: "relative" }}
        onClick={onContentClick}
      >
        {isPlaceholderData && (
          <div
            aria-hidden
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              right: 0,
              height: 2,
              background: token.colorPrimary,
            }}
          />
        )}
        {contentError && !isPlaceholderData && (
          <Alert
            type="error"
            title="Failed to load doc"
            description={String(contentError)}
            showIcon
            style={{ marginBottom: 16 }}
          />
        )}
        {contentLoading && !content ? (
          <div style={{ padding: 48, textAlign: "center" }}>
            <Spin />
          </div>
        ) : content ? (
          <div style={{ maxWidth: 800 }}>
            <MarkdownWithMermaid content={content} darkMode={darkMode} />
          </div>
        ) : (
          <Typography.Text type="secondary">Select a document from the sidebar.</Typography.Text>
        )}
      </Content>
      {searchActive ? (
        <Sider
          width={400}
          collapsedWidth={48}
          collapsible
          collapsed={resultsCollapsed}
          onCollapse={setResultsCollapsed}
          trigger={null}
          style={{
            background: "transparent",
            borderLeft: `1px solid ${token.colorBorder}`,
            paddingTop: 8,
            color: token.colorText,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: resultsCollapsed ? "center" : "space-between",
              margin: "0 8px 8px",
              gap: 8,
            }}
          >
            {resultsCollapsed ? (
              <Tooltip title={resultsLabel} placement="left">
                <Button
                  type="text"
                  icon={<MenuUnfoldOutlined />}
                  onClick={() => setResultsCollapsed(false)}
                  aria-label="Expand search results"
                />
              </Tooltip>
            ) : (
              <>
                <Title
                  level={5}
                  style={{
                    margin: 0,
                    padding: "0 4px",
                    color: token.colorText,
                    flex: 1,
                  }}
                >
                  {resultsLabel}
                </Title>
                <Button
                  type="text"
                  icon={<MenuFoldOutlined />}
                  onClick={() => setResultsCollapsed(true)}
                  aria-label="Collapse search results"
                />
              </>
            )}
          </div>
          {resultsCollapsed ? null : (
          <div style={{ maxHeight: "calc(100vh - 160px)", overflowY: "auto", padding: "0 8px 16px" }}>
            {searchLoading && searchHits.length === 0 ? (
              <div style={{ padding: 16, textAlign: "center" }}>
                <Spin size="small" />
              </div>
            ) : searchHits.length === 0 ? (
              <Typography.Text
                type="secondary"
                style={{ display: "block", padding: "8px 12px", fontSize: 13 }}
              >
                No matches
              </Typography.Text>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {searchHits.map((hit) => {
                  const selected = hit.id === currentId;
                  return (
                    <div
                      key={hit.id}
                      role="button"
                      tabIndex={0}
                      style={{
                        cursor: "pointer",
                        background: selected ? token.colorPrimaryBg : undefined,
                        color: selected ? token.colorPrimaryText : token.colorText,
                        borderRadius: 4,
                        padding: "10px 12px",
                      }}
                      onMouseEnter={() => prefetchDoc(hit.id)}
                      onFocus={() => prefetchDoc(hit.id)}
                      onClick={() => navigate(`/developer-docs/${hit.id}`)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          navigate(`/developer-docs/${hit.id}`);
                        }
                      }}
                    >
                      <div style={{ fontSize: 14, fontWeight: 500 }}>{hit.title}</div>
                      <div
                        style={{
                          fontSize: 12,
                          color: token.colorTextSecondary,
                          marginTop: 2,
                        }}
                      >
                        {hit.section_title}
                        {hit.heading ? ` · ${hit.heading}` : ""}
                      </div>
                      {hit.snippet ? (
                        <div
                          style={{
                            fontSize: 13,
                            color: token.colorTextSecondary,
                            marginTop: 6,
                            lineHeight: 1.45,
                          }}
                        >
                          {hit.snippet}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
          )}
        </Sider>
      ) : null}
    </Layout>
  );
}
