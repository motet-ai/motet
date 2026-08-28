/**
 * Motet UI Common - Attachment Types
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-06-15
 *
 * Description:
 *     Type definitions and helper functions for file attachments in chat messages.
 *
 *     Types:
 *     - AttachmentState: Full attachment state including derived artifacts
 *     - PresetIcons: Icon type names for Ant Design X FileCard
 *     - DraftUploadItem: In-progress upload before server response
 *
 *     Functions:
 *     - inferPresetIcon(): Maps MIME types to icon names
 *     - inferFileCardProps(): Converts AttachmentState to FileCard props
 *     - formatVideoDerivationProcessingStep(): Per-track video derivation progress label
 *     - formatVideoDerivationReadyDescription(): Ready-state label for video attachments
 *
 *     Attachment Lifecycle:
 *     1. pending: File selected but not uploaded
 *     2. uploading: Upload in progress
 *     3. uploaded: Upload complete, waiting for processing
 *     4. processing: Derivation pipeline running (text extraction, OCR)
 *     5. ready: Fully processed and available
 *     6. expired: Artifact has been cleaned up
 *     7. error: Something went wrong
 *
 * Dependencies:
 *     - None (types + pure helpers)
 *
 * Usage:
 *     import { AttachmentState, inferFileCardProps } from "@motet/ui-common";
 *     const props = inferFileCardProps(attachment, { imageSrc: blobUrl });
 */

// ─────────────────────────────────────────────────────────────────────────────
// ATTACHMENT STATE
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Represents the state of an uploaded file attachment.
 * Tracks the artifact ID, metadata, processing status, and derived outputs.
 */
export interface AttachmentState {
  /** Server-assigned artifact ID (UUID) */
  artifact_id: string;
  /** Original filename from upload */
  filename: string;
  /** MIME type (e.g., "image/png", "application/pdf") */
  content_type: string;
  /** File size in bytes */
  bytes: number;
  /** Current lifecycle status */
  status: "pending" | "uploading" | "uploaded" | "processing" | "ready" | "expired" | "error";
  /** IDs of derived artifacts from processing pipeline */
  derived_artifact_ids?: {
    /** Extracted text content from documents */
    extracted_text?: string;
    /** OCR text from images/scanned documents */
    ocr_text?: string;
    /** Page images for multi-page documents */
    page_images?: string[];
  };
  /** Current processing step description (e.g., "Generating thumb...", "Extracting text...") */
  processing_step?: string;
  /** Processing progress percentage (0-100) */
  processing_progress?: number;
  /** Error message if status is "error" */
  error?: string;
  /** Per-track video derivation status (ADR-0119 parallel visuals + transcript) */
  video_derivation?: VideoDerivationStatus;
}

/** Status of one video derivation track (keyframes or transcript). */
export type VideoDerivationTrackStatus = "pending" | "complete" | "skipped" | "failed";

/** Parallel video derivation tracks tracked independently in the UI. */
export interface VideoDerivationStatus {
  visuals: VideoDerivationTrackStatus;
  transcript: VideoDerivationTrackStatus;
}

/** Initial video derivation state when upload completes and both tracks are queued. */
export function initialVideoDerivationStatus(): VideoDerivationStatus {
  return { visuals: "pending", transcript: "pending" };
}

/** Map derivation completion event status to a track status for display. */
export function videoDerivationTrackFromEventStatus(eventStatus: string | undefined): VideoDerivationTrackStatus {
  if (eventStatus === "success") return "complete";
  if (eventStatus === "error") return "failed";
  return "skipped";
}

function videoTrackProcessingLabel(
  track: "visuals" | "transcript",
  status: VideoDerivationTrackStatus
): string {
  if (track === "visuals") {
    if (status === "pending") return "Extracting keyframes…";
    if (status === "complete") return "Keyframes ✓";
    if (status === "skipped") return "Keyframes skipped";
    return "Keyframes failed";
  }
  if (status === "pending") return "Transcribing…";
  if (status === "complete") return "Transcript ✓";
  if (status === "skipped") return "Transcript skipped";
  return "Transcript failed";
}

/** FileCard subtitle while video derivations are in progress. */
export function formatVideoDerivationProcessingStep(video: VideoDerivationStatus): string {
  return `${videoTrackProcessingLabel("visuals", video.visuals)} · ${videoTrackProcessingLabel("transcript", video.transcript)}`;
}

/** FileCard subtitle when a video attachment is ready (playback + AI context tracks). */
export function formatVideoDerivationReadyDescription(video: VideoDerivationStatus): string {
  if (video.visuals === "complete" && video.transcript === "complete") {
    return "Ready for AI context";
  }
  const parts: string[] = [];
  if (video.visuals === "complete") parts.push("Keyframes ready");
  else if (video.visuals === "skipped") parts.push("Keyframes skipped");
  else if (video.visuals === "failed") parts.push("Keyframes failed");
  if (video.transcript === "complete") parts.push("Transcript ready");
  else if (video.transcript === "skipped") parts.push("Transcript skipped");
  else if (video.transcript === "failed") parts.push("Transcript failed");
  if (parts.length === 0) return "Ready for playback";
  return parts.join(" · ");
}

// ─────────────────────────────────────────────────────────────────────────────
// DRAFT UPLOAD
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Represents an in-progress file upload before server response.
 */
export type DraftUploadItem = {
  uid: string;
  name: string;
  size: number;
  type: string;
  status: "uploading" | "error";
  percent?: number;
  error?: string;
};

// ─────────────────────────────────────────────────────────────────────────────
// ICON TYPES
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Preset icon names supported by Ant Design X FileCard.
 * Used to display appropriate icons for different file types.
 */
export type PresetIcons = "image" | "pdf" | "excel" | "word" | "ppt" | "zip" | "video" | "audio" | "markdown" | "javascript" | "python" | "java" | "default";

// ─────────────────────────────────────────────────────────────────────────────
// HELPER FUNCTIONS
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Maps a MIME type to an appropriate preset icon name.
 *
 * @param contentType - MIME type string (e.g., "image/png")
 * @returns PresetIcons name for FileCard
 */
export function inferPresetIcon(contentType: string): PresetIcons {
  if (contentType.startsWith("image/")) return "image";
  if (contentType === "application/pdf") return "pdf";
  if (contentType.includes("spreadsheet") || contentType.includes("excel")) return "excel";
  if (contentType.includes("wordprocessing") || contentType.includes("word")) return "word";
  if (contentType.includes("presentation") || contentType.includes("powerpoint")) return "ppt";
  if (contentType.includes("zip") || contentType.includes("archive")) return "zip";
  if (contentType.startsWith("video/")) return "video";
  if (contentType.startsWith("audio/")) return "audio";
  if (contentType.includes("markdown")) return "markdown";
  if (contentType.includes("javascript")) return "javascript";
  if (contentType.includes("python")) return "python";
  if (contentType.includes("java")) return "java";
  return "default";
}

/**
 * Converts an AttachmentState to props for Ant Design X FileCard.
 *
 * @param attachment - The attachment state to convert
 * @param opts - Optional settings
 * @param opts.imageSrc - Pre-authenticated blob URL for image preview
 * @returns Props object suitable for FileCard component
 *
 * @note IMPORTANT: For images, callers must provide an authenticated blob URL
 *       via opts.imageSrc. We do NOT default to the preview API endpoint because
 *       it requires auth headers that can't be sent with img src attributes.
 */
export function inferFileCardProps(attachment: AttachmentState, opts?: { imageSrc?: string }) {
  const {
    filename,
    content_type,
    bytes,
    status,
    processing_step,
    processing_progress,
    derived_artifact_ids,
    video_derivation,
  } = attachment;

  const type = content_type.startsWith("image/") ? "image" : "file";
  const icon = inferPresetIcon(content_type);
  const src = type === "image" ? opts?.imageSrc : undefined;

  let description = "";
  let percent: number | undefined = undefined;

  if (status === "uploading") {
    description = "Uploading...";
  } else if (status === "processing") {
    description = processing_step || "Processing...";
    if (processing_progress !== undefined) {
      percent = processing_progress;
    }
  } else if (status === "error") {
    description = "Error";
  } else if (status === "expired") {
    description = "Expired";
  } else if (content_type.startsWith("video/") && video_derivation) {
    description = formatVideoDerivationReadyDescription(video_derivation);
  } else if (derived_artifact_ids?.extracted_text) {
    description = "Text extracted";
  }

  return {
    name: filename,
    byte: bytes,
    type,
    icon,
    src,
    status: status as any,
    description,
    percent,
  };
}
