/**
 * Motet UI Common - Mermaid Diagram Rendering
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-06-02
 *
 * Description:
 *     Components for rendering Mermaid diagrams within markdown content.
 *
 *     Components:
 *     1. MermaidBlock: Renders a single Mermaid diagram with Image/Code toggle.
 *     2. renderMarkdownWithMermaid: Parses markdown and extracts ```mermaid```
 *        fenced blocks, rendering them with MermaidBlock while rendering the
 *        rest with standard Markdown. Optionally resolves `artifact:<id>` image
 *        links (ADR-0113) to controlled <img> elements via MediaRenderer, since
 *        the markdown sanitizer strips non-standard URI schemes from `src`.
 *
 * Dependencies:
 *     - @ant-design/x: Mermaid component for diagram rendering
 *     - @ant-design/x-markdown: Markdown component for text rendering
 *     - antd: Segmented for mode toggle
 *
 * Usage:
 *     import { MermaidBlock, renderMarkdownWithMermaid } from "@motet/ui-common/components";
 *
 *     <MermaidBlock code="graph LR; A-->B" />
 *     {renderMarkdownWithMermaid(content)}
 */
import React, { useState, useRef, useEffect } from "react";
import { Segmented } from "antd";
import { Mermaid } from "@ant-design/x";
import Markdown from "@ant-design/x-markdown";
import { MediaRenderer } from "../MediaRenderer";

/**
 * Options for {@link renderMarkdownWithMermaid} that enable resolving
 * `artifact:<id>` Markdown image links to displayable bytes (ADR-0113).
 *
 * Generated images are referenced by agents as `![alt](artifact:<artifact_id>)`.
 * Because the markdown engine's HTML sanitizer strips non-standard URI schemes
 * (`artifact:`, and even `blob:`) from `src`, those images cannot be rendered by
 * passing the URL through Markdown. Instead, such links are tokenized out and
 * rendered as controlled React <img> elements via {@link MediaRenderer}, using a
 * host-resolved blob URL (the host holds auth and fetches the preview bytes).
 */
export interface MarkdownRenderOptions {
  /** Resolve a displayable URL for an artifact-backed image (undefined while loading). */
  resolveImageUrl?: (artifactId: string) => string | undefined;
  /** Request the host fetch an image artifact's preview bytes (called when unresolved). */
  onRequestImage?: (artifactId: string) => void | Promise<unknown>;
}

// Matches Markdown image links that use the artifact URI scheme:
//   ![alt text](artifact:a1c250a1-7a1a-4cb5-9d60-04e7569f20dd)
const ARTIFACT_IMAGE_RE = /!\[([^\]]*)\]\(\s*artifact:([^)\s]+)\s*\)/g;

/**
 * Renders a plain (non-mermaid) markdown segment, tokenizing out artifact-backed
 * media references and rendering them as controlled elements via {@link MediaRenderer}
 * (which bypasses the markdown sanitizer that strips `artifact:`/`blob:` URLs).
 * When no artifact options are provided, the segment is rendered as-is through Markdown.
 *
 * The mechanism is media-general, but the currently *resolved* type is images
 * referenced as `![alt](artifact:<id>)`. Supporting other media (audio/video/file)
 * additionally requires matching the link form `[label](artifact:<id>)`, resolving
 * the artifact's mime type, and audio/video branches in {@link MediaRenderer}.
 */
function renderTextWithArtifactMedia(
  text: string,
  keyPrefix: string,
  darkMode: boolean,
  options?: MarkdownRenderOptions
): React.ReactNode[] {
  if (!options || (!options.resolveImageUrl && !options.onRequestImage)) {
    return [<Markdown key={keyPrefix}>{text}</Markdown>];
  }

  const out: React.ReactNode[] = [];
  const re = new RegExp(ARTIFACT_IMAGE_RE.source, "g");
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  let i = 0;

  while ((match = re.exec(text)) !== null) {
    const before = text.slice(lastIdx, match.index);
    if (before.length > 0) {
      out.push(<Markdown key={`${keyPrefix}:t${i}`}>{before}</Markdown>);
    }
    const alt = match[1] || "generated image";
    const artifactId = match[2];
    out.push(
      <MediaRenderer
        key={`${keyPrefix}:img${i}`}
        media={[{ type: "media", media_type: "image", mime_type: "image/png", artifact_id: artifactId, alt }]}
        resolveImageUrl={options.resolveImageUrl}
        onRequestImage={options.onRequestImage}
        darkMode={darkMode}
      />
    );
    lastIdx = re.lastIndex;
    i++;
  }

  const tail = text.slice(lastIdx);
  if (tail.length > 0) {
    out.push(<Markdown key={`${keyPrefix}:t${i}`}>{tail}</Markdown>);
  }
  if (out.length === 0) {
    out.push(<Markdown key={keyPrefix}>{text}</Markdown>);
  }
  return out;
}

/**
 * Renders a Mermaid diagram with a toggle to switch between visual and code view.
 */
export function MermaidBlock({ code, darkMode = false }: { code: string; darkMode?: boolean }): React.ReactNode {
  const [mode, setMode] = useState<"image" | "code">("image");
  const containerRef = useRef<HTMLDivElement>(null);
  const trimmed = (code || "").trim();
  if (!trimmed) return null;

  // Force white background on SVG in dark mode after render.
  // MutationObserver catches SVG when it's rendered asynchronously.
  useEffect(() => {
    if (!darkMode || mode !== "image" || !containerRef.current) return;

    const fixSvgBackground = () => {
      const svg = containerRef.current?.querySelector("svg");
      if (svg) {
        svg.style.backgroundColor = "#ffffff";
        svg.style.background = "#ffffff";

        const viewBox = svg.getAttribute("viewBox");
        if (viewBox) {
          const [x, y, width, height] = viewBox.split(" ").map(Number);
          let bgRect = svg.querySelector("rect.mermaid-bg-override");
          if (!bgRect) {
            bgRect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
            bgRect.setAttribute("class", "mermaid-bg-override");
            bgRect.setAttribute("fill", "#ffffff");
            svg.insertBefore(bgRect, svg.firstChild);
          }
          bgRect.setAttribute("x", String(x));
          bgRect.setAttribute("y", String(y));
          bgRect.setAttribute("width", String(width));
          bgRect.setAttribute("height", String(height));
          bgRect.setAttribute("fill", "#ffffff");
        }

        const rects = svg.querySelectorAll("rect:not(.mermaid-bg-override)");
        rects.forEach((rect) => {
          const fill = rect.getAttribute("fill");
          if (fill && (fill === "#000000" || fill === "#1e1e1e" || fill === "#141414" || fill === "rgb(0, 0, 0)" || fill === "rgb(30, 30, 30)" || fill.startsWith("rgb(0,") || fill.startsWith("rgb(30,"))) {
            rect.setAttribute("fill", "#ffffff");
          }
          const w = rect.getAttribute("width");
          const h = rect.getAttribute("height");
          if (w && h) {
            const wN = parseFloat(w);
            const hN = parseFloat(h);
            if ((w === "100%" || wN > 100) && (h === "100%" || hN > 100)) {
              rect.setAttribute("fill", "#ffffff");
            }
          }
        });
        return true;
      }
      return false;
    };

    if (fixSvgBackground()) return;

    const observer = new MutationObserver(() => {
      if (fixSvgBackground()) {
        observer.disconnect();
      }
    });

    observer.observe(containerRef.current, {
      childList: true,
      subtree: true,
    });

    const timeout = setTimeout(() => {
      fixSvgBackground();
      observer.disconnect();
    }, 500);

    return () => {
      observer.disconnect();
      clearTimeout(timeout);
    };
  }, [darkMode, mode, trimmed]);

  return (
    <div className="mermaid-block" style={{ margin: "8px 0" }}>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 6 }}>
        <Segmented
          size="small"
          options={[
            { label: "Image", value: "image" },
            { label: "Code", value: "code" },
          ]}
          value={mode}
          onChange={(v) => setMode(v as "image" | "code")}
        />
      </div>
      {mode === "image" ? (
        <div
          ref={containerRef}
          className={`mermaid-block-render ${darkMode ? "mermaid-dark-mode" : ""}`}
          style={{
            backgroundColor: darkMode ? "#ffffff" : "transparent",
            overflow: "visible"
          }}
        >
          <Mermaid header={null}>{trimmed}</Mermaid>
        </div>
      ) : (
        <Markdown>{`\`\`\`mermaid\n${trimmed}\n\`\`\``}</Markdown>
      )}
    </div>
  );
}

/**
 * Parses markdown content and renders Mermaid fenced blocks with MermaidBlock,
 * while rendering the rest with standard Markdown.
 */
export function renderMarkdownWithMermaid(
  markdown: string,
  darkMode: boolean = false,
  options?: MarkdownRenderOptions
): React.ReactNode {
  if (!markdown || typeof markdown !== "string") return <Markdown>{""}</Markdown>;

  const parts: React.ReactNode[] = [];
  const re = /```mermaid\s*\n([\s\S]*?)```/g;
  let lastIdx = 0;
  let match: RegExpExecArray | null;

  while ((match = re.exec(markdown)) !== null) {
    const start = match.index;
    const end = re.lastIndex;

    const before = markdown.slice(lastIdx, start);
    if (before.trim().length > 0) {
      parts.push(...renderTextWithArtifactMedia(before, `md:${lastIdx}`, darkMode, options));
    } else if (before.length > 0) {
      parts.push(<div key={`sp:${lastIdx}`} />);
    }

    const mermaidCode = (match[1] || "").trim();
    if (mermaidCode) {
      parts.push(<MermaidBlock key={`mm:${start}`} code={mermaidCode} darkMode={darkMode} />);
    } else {
      parts.push(<Markdown key={`md-empty:${start}`}>{"```mermaid\n\n```"}</Markdown>);
    }

    lastIdx = end;
  }

  const tail = markdown.slice(lastIdx);
  if (tail.trim().length > 0) {
    parts.push(...renderTextWithArtifactMedia(tail, `md:${lastIdx}`, darkMode, options));
  }

  if (parts.length === 0) {
    return <div>{renderTextWithArtifactMedia(markdown, "md:0", darkMode, options)}</div>;
  }

  return <div>{parts}</div>;
}
