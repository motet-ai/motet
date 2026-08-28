/**
 * Motet UI Common - Media Renderer
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-06-10
 *
 * Description:
 *     Framework-shared, presentational renderer for canonical media parts
 *     (ADR-0064 / ADR-0113 / ADR-0118). Given a list of `MediaPart`s (e.g.
 *     assistant-generated images surfaced on the chat turn `end`), it renders
 *     each part by media type:
 *       - image: <img> from a resolved blob/URL; shows a placeholder while loading
 *       - video: <video controls> from a resolved playback/stream URL (ADR-0118
 *         Phase A.2); shows a placeholder while the playback URL is minted
 *       - other: a compact label (download requires auth, handled by the host app)
 *
 *     The component is intentionally auth-agnostic: artifact bytes are resolved by
 *     the host application (which holds the JWT/session) via `resolveImageUrl` /
 *     `onRequestImage` (blob URLs from the preview endpoint) and `resolveVideoUrl` /
 *     `onRequestVideo` (tokenized stream URLs from the playback-token endpoint —
 *     blob URLs are NOT used for video because they defeat Range/seek streaming).
 *
 * Dependencies:
 *     - react
 *     - MediaPart type (../api/chat)
 *
 * Usage:
 *     <MediaRenderer
 *       media={message.media}
 *       resolveImageUrl={(id) => imageBlobUrls.get(id)}
 *       onRequestImage={ensureImagePreview}
 *       darkMode={darkMode}
 *     />
 */
import React, { useEffect } from "react";
import type { MediaPart } from "../api/chat";

/** Props for {@link MediaRenderer}. */
export interface MediaRendererProps {
  /** Canonical media parts to render (artifact-backed or direct URL). */
  media: MediaPart[] | undefined;
  /**
   * Resolve a displayable URL for an artifact-backed image. Return undefined
   * while the preview is still loading (the host typically caches blob URLs).
   */
  resolveImageUrl?: (artifactId: string) => string | undefined;
  /**
   * Request that the host fetch an image artifact's preview bytes. Called once
   * per image artifact that lacks a resolved URL. The host should update its
   * cache so a subsequent render resolves the URL.
   */
  onRequestImage?: (artifactId: string) => void | Promise<unknown>;
  /**
   * Resolve a streamable playback URL for an artifact-backed video (ADR-0118:
   * tokenized /stream URL, NOT a blob URL). Return undefined while minting.
   */
  resolveVideoUrl?: (artifactId: string) => string | undefined;
  /**
   * Request that the host mint a playback URL for a video artifact. Called once
   * per video artifact that lacks a resolved URL.
   */
  onRequestVideo?: (artifactId: string) => void | Promise<unknown>;
  /** Dark mode toggles placeholder/border colors. */
  darkMode?: boolean;
  /** Optional max rendered image height in px (default 384). */
  maxImageHeight?: number;
}

function isImagePart(part: MediaPart): boolean {
  if (part.media_type && part.media_type.toLowerCase() === "image") return true;
  if (part.mime_type && part.mime_type.toLowerCase().startsWith("image/")) return true;
  return false;
}

function isVideoPart(part: MediaPart): boolean {
  if (part.media_type && part.media_type.toLowerCase() === "video") return true;
  if (part.mime_type && part.mime_type.toLowerCase().startsWith("video/")) return true;
  return false;
}

/**
 * Presentational renderer for a list of canonical media parts.
 * Returns null when there is nothing to render.
 */
export function MediaRenderer({
  media,
  resolveImageUrl,
  onRequestImage,
  resolveVideoUrl,
  onRequestVideo,
  darkMode = false,
  maxImageHeight = 384,
}: MediaRendererProps): React.ReactElement | null {
  const parts = Array.isArray(media) ? media.filter((m) => m && (m.artifact_id || m.url)) : [];

  // Request previews (images) / playback URLs (videos) for artifacts that
  // don't yet have a resolved URL.
  useEffect(() => {
    for (const part of parts) {
      const id = part.artifact_id;
      if (!id || part.url) continue;
      if (isImagePart(part)) {
        if (onRequestImage && !resolveImageUrl?.(id)) void onRequestImage(id);
      } else if (isVideoPart(part)) {
        if (onRequestVideo && !resolveVideoUrl?.(id)) void onRequestVideo(id);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parts.map((p) => p.artifact_id || p.url).join(","), onRequestImage, resolveImageUrl, onRequestVideo, resolveVideoUrl]);

  if (parts.length === 0) return null;

  const borderColor = darkMode ? "#303030" : "#d9d9d9";
  const placeholderBg = darkMode ? "#1f1f1f" : "#fafafa";
  const placeholderFg = darkMode ? "#8c8c8c" : "#8c8c8c";

  return (
    <div
      className="motet-media-renderer"
      style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 8 }}
    >
      {parts.map((part, idx) => {
        const key = part.artifact_id || part.url || `media-${idx}`;
        if (isImagePart(part)) {
          const src = part.url || (part.artifact_id ? resolveImageUrl?.(part.artifact_id) : undefined);
          const alt = part.alt || part.filename || "generated image";
          if (src) {
            return (
              <img
                key={key}
                src={src}
                alt={alt}
                style={{
                  maxWidth: "100%",
                  maxHeight: maxImageHeight,
                  borderRadius: 8,
                  border: `1px solid ${borderColor}`,
                  objectFit: "contain",
                  display: "block",
                }}
              />
            );
          }
          return (
            <div
              key={key}
              style={{
                width: 192,
                height: 144,
                borderRadius: 8,
                border: `1px dashed ${borderColor}`,
                background: placeholderBg,
                color: placeholderFg,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 12,
              }}
            >
              Loading image…
            </div>
          );
        }
        if (isVideoPart(part)) {
          const src = part.url || (part.artifact_id ? resolveVideoUrl?.(part.artifact_id) : undefined);
          if (src) {
            return (
              <video
                key={key}
                src={src}
                controls
                preload="metadata"
                style={{
                  maxWidth: "100%",
                  maxHeight: maxImageHeight,
                  borderRadius: 8,
                  border: `1px solid ${borderColor}`,
                  display: "block",
                  background: "#000",
                }}
              />
            );
          }
          return (
            <div
              key={key}
              style={{
                width: 256,
                height: 144,
                borderRadius: 8,
                border: `1px dashed ${borderColor}`,
                background: placeholderBg,
                color: placeholderFg,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 12,
              }}
            >
              Preparing video…
            </div>
          );
        }
        // Other media: compact label (download needs host auth).
        const label = part.filename || part.mime_type || part.media_type || "attachment";
        return (
          <div
            key={key}
            style={{
              padding: "6px 10px",
              borderRadius: 8,
              border: `1px solid ${borderColor}`,
              background: placeholderBg,
              color: darkMode ? "#d9d9d9" : "#595959",
              fontSize: 12,
            }}
          >
            📎 {label}
          </div>
        );
      })}
    </div>
  );
}

export default MediaRenderer;
