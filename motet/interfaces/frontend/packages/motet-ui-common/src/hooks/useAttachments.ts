/**
 * Motet UI Common - Attachments Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-06-15
 *
 * Description:
 *     Comprehensive file attachment management for chat UIs. Handles the full
 *     lifecycle of file attachments from upload to cleanup.
 *
 *     Upload Flow:
 *     1. User selects/drops file -> draftUploads tracks progress
 *     2. File uploaded to /api/v1/artifacts?conversation_id={id} -> returns artifact metadata
 *     3. Artifact added to attachmentList with status "processing" -> derivations triggered
 *     4. Listen to derivation completion events -> update status to "ready" when complete
 *     5. For images, blob URL fetched from /api/v1/artifacts/{id}/preview
 *     6. For videos, a tokenized stream URL is minted via
 *        POST /api/v1/artifacts/{id}/playback-token (ADR-0118 Phase A.2) —
 *        NOT a blob URL, so <video> elements get HTTP Range streaming/seek
 *
 *     Derivation Tracking:
 *     - Subscribes to SSE events for derivation completion
 *     - Tracks expected derivations per artifact based on content_type
 *     - Updates attachment status from "processing" to "ready" when all derivations complete
 *     - Images: Expect "image" derivation (thumb, base, detail)
 *     - Documents: Expect "text" derivation (extracted text)
 *     - Videos: Expect "video_visuals" (poster/keyframes) and "video_transcript"
 *       derivations, which run in parallel and complete independently (ADR-0119)
 *
 *     Memory Management:
 *     - Blob URLs are cached in imageBlobUrls Map
 *     - Blob URLs are revoked when attachments are removed (removeBlobUrl)
 *     - All blob URLs are revoked on component unmount
 *     - Server artifacts are deleted best-effort when removed (deleteArtifactBestEffort)
 *
 *     Deduplication:
 *     - fetchPromiseRef prevents parallel fetches for the same artifact
 *     - Ensures only one request is in-flight per artifact ID
 *
 * Dependencies:
 *     - Browser APIs: fetch, FormData, URL.createObjectURL, URL.revokeObjectURL
 *     - React: useState, useRef, useEffect, useCallback, useMemo
 *     - useAuth: buildAuthHeaders, buildHeaders for authenticated API calls
 *
 * Usage:
 *     import { useAttachments } from "@motet/ui-common";
 *     const { fileCardList, handleUpload, ensureImagePreview } = useAttachments(auth, conversationId);
 *
 * Notes:
 *     - fileCardList combines draftUploads (in-progress) with attachmentList (completed)
 *     - Images show "uploading" status until blob URL is loaded
 *     - Attachments show "processing" status until derivations complete
 *     - Server deletion is best-effort (errors logged but not surfaced to user)
 */
import { useState, useRef, useEffect, useCallback, useMemo } from "react";
import type { AttachmentState, DraftUploadItem, VideoDerivationStatus } from "../types/attachments";
import {
  inferFileCardProps,
  initialVideoDerivationStatus,
  formatVideoDerivationProcessingStep,
  videoDerivationTrackFromEventStatus,
} from "../types/attachments";
import type { AuthState } from "../types";
import { parseSseBuffer } from "../utils";
import { buildAuthHeaders, buildHeaders } from "./useAuth";

/**
 * React hook for managing file attachments in the chat UI.
 *
 * @param auth - Current auth state for constructing authenticated headers
 * @param conversationId - Optional conversation ID to associate uploads with
 * @param overrides - Optional model overrides for derivation alignment
 */
export function useAttachments(auth: AuthState, conversationId?: string, overrides?: any) {
  // ─────────────────────────────────────────────────────────────────────────────
  // STATE: Attachment data and UI state
  // ─────────────────────────────────────────────────────────────────────────────

  const [attachmentList, setAttachmentList] = useState<AttachmentState[]>([]);
  const [imageBlobUrls, setImageBlobUrls] = useState<Map<string, string>>(new Map());
  const imageBlobUrlsRef = useRef<Map<string, string>>(new Map());
  const fetchPromiseRef = useRef<Map<string, Promise<string | undefined>>>(new Map());
  // ADR-0118: tokenized stream URLs (NOT blob URLs) keyed by artifact id, with expiry tracking.
  const [videoStreamUrls, setVideoStreamUrls] = useState<Map<string, string>>(new Map());
  const videoStreamExpiryRef = useRef<Map<string, number>>(new Map());
  const videoFetchPromiseRef = useRef<Map<string, Promise<string | undefined>>>(new Map());
  const [showAttachments, setShowAttachments] = useState(false);
  const [draftUploads, setDraftUploads] = useState<DraftUploadItem[]>([]);

  const expectedDerivationsRef = useRef<Map<string, Set<string>>>(new Map());
  const totalStepsRef = useRef<Map<string, number>>(new Map());
  const completedStepsRef = useRef<Map<string, number>>(new Map());
  const pdfMetadataRef = useRef<Map<string, { totalPages?: number; ocrPagesCompleted: number }>>(new Map());
  const derivationTimeoutsRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const artifactMetadataRef = useRef<Map<string, { content_type: string; filename: string; bytes: number }>>(new Map());

  // ─────────────────────────────────────────────────────────────────────────────
  // CONSTANTS
  // ─────────────────────────────────────────────────────────────────────────────

  const DERIVATION_TIMEOUT_MS = 60000;
  const PDF_DERIVATION_TIMEOUT_MS = 300000;
  const IMAGE_DERIVATION_STEPS = 3;

  const getDerivationTimeoutMs = useCallback((contentType: string): number => {
    return contentType === "application/pdf" ? PDF_DERIVATION_TIMEOUT_MS : DERIVATION_TIMEOUT_MS;
  }, []);

  const resolvePdfTotalPagesFromEvent = useCallback((eventData: any): number | undefined => {
    const candidates = [
      eventData.total_pages,
      eventData.result?.data?.total_pages,
      eventData.result?.total_pages,
      eventData.page_count,
      eventData.result?.data?.page_count,
    ];
    for (const value of candidates) {
      if (typeof value === "number" && Number.isFinite(value) && value > 0) {
        return value;
      }
    }
    const pages = eventData.pages ?? eventData.result?.data?.pages;
    if (Array.isArray(pages) && pages.length > 0) {
      return pages.length;
    }
    return undefined;
  }, []);

  const resolveOcrPageNumFromEvent = useCallback((eventData: any): number | undefined => {
    const candidates = [
      eventData.page_num,
      eventData.result?.data?.page_num,
      eventData.result?.page_num,
    ];
    for (const value of candidates) {
      if (typeof value === "number" && Number.isFinite(value) && value > 0) {
        return value;
      }
    }
    return undefined;
  }, []);

  // ─────────────────────────────────────────────────────────────────────────────
  // EFFECTS: Sync refs and cleanup
  // ─────────────────────────────────────────────────────────────────────────────

  useEffect(() => {
    imageBlobUrlsRef.current = imageBlobUrls;
  }, [imageBlobUrls]);

  // ─────────────────────────────────────────────────────────────────────────────
  // DERIVATION HELPERS
  // ─────────────────────────────────────────────────────────────────────────────

  const getExpectedDerivations = useCallback((contentType: string): Set<string> => {
    const expected = new Set<string>();
    const isTextEligible =
      contentType === "application/pdf" ||
      contentType.includes("wordprocessing") ||
      contentType.includes("spreadsheet") ||
      contentType.startsWith("text/") ||
      contentType === "application/json";
    if (isTextEligible) expected.add("text");
    if (contentType.startsWith("image/")) expected.add("image");
    if (contentType.startsWith("video/")) {
      // ADR-0119: visuals and transcript derive in parallel and emit
      // independent completion events; track each as its own derivation.
      expected.add("video_visuals");
      expected.add("video_transcript");
    }
    return expected;
  }, []);

  const getTotalStepsForDerivation = useCallback((derivationType: string, contentType?: string): number => {
    if (derivationType === "image") return IMAGE_DERIVATION_STEPS;
    if (derivationType === "video_visuals" || derivationType === "video_transcript") return 1;
    if (derivationType === "text") {
      if (contentType === "application/pdf") return 10;
      return 2;
    }
    return 0;
  }, []);

  const getSourceArtifactId = useCallback((data: any): string | undefined => {
    return data.source_artifact_id ||
      data.result?.data?.source_artifact_id ||
      data.result?.source_artifact_id ||
      data.command_data?.source_artifact_id;
  }, []);

  const getArtifactId = useCallback((data: any): string | undefined => {
    return data.artifact_id ||
      data.result?.data?.artifact_id ||
      data.result?.artifact_id;
  }, []);

  const calculateTotalSteps = useCallback((expected: Set<string>, contentType?: string): number => {
    let total = 0;
    expected.forEach((derivationType) => {
      total += getTotalStepsForDerivation(derivationType, contentType);
    });
    return total;
  }, [getTotalStepsForDerivation]);

  const initializeTrackingRefs = useCallback((
    artifactId: string,
    contentType: string,
    expected?: Set<string>
  ): void => {
    const derivations = expected ?? getExpectedDerivations(contentType);
    if (derivations.size > 0) {
      expectedDerivationsRef.current.set(artifactId, derivations);
      const totalSteps = calculateTotalSteps(derivations, contentType);
      totalStepsRef.current.set(artifactId, totalSteps);
      if (derivations.has("text") && contentType !== "application/pdf" && totalSteps >= 2) {
        completedStepsRef.current.set(artifactId, 1);
      } else {
        completedStepsRef.current.set(artifactId, 0);
      }
      if (contentType === "application/pdf") {
        pdfMetadataRef.current.set(artifactId, { ocrPagesCompleted: 0 });
      }
    }
  }, [getExpectedDerivations, calculateTotalSteps]);

  const markDerivationComplete = useCallback((
    artifactId: string,
    derivationType: string,
    stepsCompleted: number
  ): boolean => {
    const expected = expectedDerivationsRef.current.get(artifactId);
    if (!expected?.has(derivationType)) return false;
    const totalSteps = totalStepsRef.current.get(artifactId) || 0;
    const currentCompleted = completedStepsRef.current.get(artifactId) || 0;
    const newCompleted = Math.min(currentCompleted + stepsCompleted, totalSteps);
    completedStepsRef.current.set(artifactId, newCompleted);
    expected.delete(derivationType);
    expectedDerivationsRef.current.set(artifactId, expected);
    return expected.size === 0;
  }, []);

  const markAttachmentReady = useCallback((artifactId: string): void => {
    setAttachmentList((prev) => {
      return prev.map((att) => {
        if (att.artifact_id === artifactId) {
          return {
            ...att,
            status: "ready" as const,
            processing_step: undefined,
            processing_progress: undefined
          };
        }
        return att;
      });
    });
    expectedDerivationsRef.current.delete(artifactId);
    totalStepsRef.current.delete(artifactId);
    completedStepsRef.current.delete(artifactId);
    const timeoutId = derivationTimeoutsRef.current.get(artifactId);
    if (timeoutId) {
      clearTimeout(timeoutId);
      derivationTimeoutsRef.current.delete(artifactId);
    }
  }, []);

  const getProcessingStepDescription = useCallback((
    artifactId: string,
    expectedDerivations: Set<string>,
    completedSteps: number,
    totalSteps: number,
    contentType?: string,
    videoDerivation?: VideoDerivationStatus
  ): string => {
    const expected = Array.from(expectedDerivations);
    const pdfMeta = pdfMetadataRef.current.get(artifactId);

    if (contentType?.startsWith("video/") && videoDerivation) {
      return formatVideoDerivationProcessingStep(videoDerivation);
    }
    if (expected.includes("image")) {
      if (completedSteps === 0) return "Generating thumb...";
      if (completedSteps === 1) return "Generating base...";
      if (completedSteps === 2) return "Generating detail...";
      if (totalSteps > 0) {
        const progress = Math.round((completedSteps / totalSteps) * 100);
        return `Processing image (${progress}%)`;
      }
      return "Processing image...";
    } else if (expected.includes("text")) {
      if (contentType === "application/pdf") {
        if (pdfMeta && pdfMeta.totalPages) {
          const ocrProgress = pdfMeta.ocrPagesCompleted || 0;
          if (ocrProgress < pdfMeta.totalPages) {
            return `OCR page ${ocrProgress + 1}/${pdfMeta.totalPages}...`;
          }
          if (completedSteps < totalSteps) return "Finalizing text...";
        }
        if (completedSteps === 0) return "Rasterizing PDF pages…";
        if (totalSteps > 0) {
          const progress = Math.round((completedSteps / totalSteps) * 100);
          return `Extracting text (${progress}%)`;
        }
        return "Processing PDF…";
      }
      if (completedSteps === 0) {
        if (contentType?.includes("wordprocessing") || contentType?.includes("word")) return "Analyzing Word document...";
        if (contentType?.includes("spreadsheet") || contentType?.includes("excel")) return "Analyzing spreadsheet...";
        if (contentType?.startsWith("text/")) return "Analyzing text file...";
        if (contentType === "application/json") return "Analyzing JSON file...";
        return "Analyzing document...";
      } else if (completedSteps === 1) {
        if (contentType?.includes("wordprocessing") || contentType?.includes("word")) return "Extracting text from Word document...";
        if (contentType?.includes("spreadsheet") || contentType?.includes("excel")) return "Extracting text from spreadsheet...";
        return "Extracting text...";
      }
      return "Extracting text...";
    }
    return "Processing...";
  }, []);

  // ─────────────────────────────────────────────────────────────────────────────
  // EFFECT: Subscribe to derivation completion events
  // ─────────────────────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!auth.jwt && !auth.serviceAccountToken && !auth.apiKey) return;

    let isMounted = true;
    let eventsAbortController: AbortController | null = null;
    let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
    const RECONNECT_DELAY = 5000;

    const connect = async () => {
      if (!isMounted) return;
      const ctrl = new AbortController();
      eventsAbortController = ctrl;

      try {
        const eventKinds = "core.create_artifact_completed,core.derive_upload_image_completed,core.derive_upload_text_completed,core.derive_video_visuals_completed,core.derive_video_transcript_completed,core.derive_pdf_page_images_completed,core.ocr_image_page_completed";
        const url = `/api/v1/events?unpack_result=true&event_kinds=${encodeURIComponent(eventKinds)}`;
        const headers = buildHeaders(auth);

        const resp = await fetch(url, {
          headers: { ...headers },
          signal: ctrl.signal
        });

        if (!resp.ok) {
          if (resp.status === 401) return;
          if (isMounted) reconnectTimeout = setTimeout(connect, RECONNECT_DELAY);
          return;
        }

        if (!resp.body) return;
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (isMounted) {
          const { done, value } = await reader.read();
          if (done) {
            console.log("[Derivation Events] SSE stream ended");
            break;
          }

          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";

          for (const part of parts) {
            const evs = parseSseBuffer(part);
            evs.forEach((ev) => {
              if (ev.event === "event_bus") {
                if (ev.data?.kind?.includes("derive") || ev.data?.kind?.includes("artifact") || ev.data?.kind?.includes("ocr")) {
                  console.log("[Derivation Events] Received event:", {
                    kind: ev.data?.kind,
                    status: ev.data?.data?.status,
                    commandType: ev.data?.data?.command_type
                  });
                }
                handleDerivationEvent(ev.data);
              }
            });
          }
        }

        if (isMounted && !ctrl.signal.aborted) {
          reconnectTimeout = setTimeout(connect, RECONNECT_DELAY);
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return;
        console.error("[Derivation Events] Connection error:", err);
        if (isMounted) reconnectTimeout = setTimeout(connect, RECONNECT_DELAY);
      }
    };

    const handleDerivationEvent = (event: any) => {
      if (!event || !event.kind || !event.data) {
        console.warn("[Derivation Events] Invalid event structure:", { event });
        return;
      }

      const eventKind = event.kind;
      const eventData = event.data;

      if (eventKind === "core.derive_upload_text_completed") {
        console.log("[Derive Text] Event received:", {
          eventKind,
          status: eventData.status,
          hasSourceArtifactId: !!getSourceArtifactId(eventData),
          sourceArtifactId: getSourceArtifactId(eventData),
          eventDataKeys: Object.keys(eventData),
          resultKeys: eventData.result ? Object.keys(eventData.result) : null,
          resultDataKeys: eventData.result?.data ? Object.keys(eventData.result.data) : null
        });
      }

      // Handle create_artifact_completed
      if (eventKind === "core.create_artifact_completed" && eventData.status === "success") {
        const artifactId = getArtifactId(eventData);
        if (!artifactId) return;

        const contentType = eventData.content_type || eventData.result?.data?.content_type;
        const filename = eventData.filename || eventData.result?.data?.filename;
        const bytes = eventData.bytes || eventData.result?.data?.bytes;

        if (contentType && filename && bytes !== undefined) {
          artifactMetadataRef.current.set(artifactId, { content_type: contentType, filename, bytes });
        }

        setAttachmentList((prev) => {
          const existing = prev.find(att => att.artifact_id === artifactId);

          if (existing) {
            const trackedExpected = expectedDerivationsRef.current.get(artifactId);
            if (trackedExpected) {
              const totalSteps = totalStepsRef.current.get(artifactId) || 0;
              const completedSteps = completedStepsRef.current.get(artifactId) || 0;

              if (trackedExpected.size === 0) {
                return prev.map((att) => {
                  if (att.artifact_id === artifactId) {
                    return { ...att, status: "ready" as const, processing_step: undefined, processing_progress: undefined };
                  }
                  return att;
                });
              }

              const stepDescription = getProcessingStepDescription(
                artifactId,
                trackedExpected,
                completedSteps,
                totalSteps,
                existing.content_type,
                existing.video_derivation
              );
              const progress = totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0;

              return prev.map((att) => {
                if (att.artifact_id === artifactId) {
                  return { ...att, status: "processing" as const, processing_step: stepDescription, processing_progress: progress };
                }
                return att;
              });
            }

            const expected = getExpectedDerivations(existing.content_type);
            if (expected.size > 0) {
              initializeTrackingRefs(artifactId, existing.content_type, expected);
              const totalSteps = totalStepsRef.current.get(artifactId) || 0;
              const completedSteps = completedStepsRef.current.get(artifactId) || 0;
              const stepDescription = getProcessingStepDescription(
                artifactId,
                expected,
                completedSteps,
                totalSteps,
                existing.content_type,
                existing.video_derivation
              );
              const progress = totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0;

              return prev.map((att) => {
                if (att.artifact_id === artifactId) {
                  return { ...att, status: "processing" as const, processing_step: stepDescription, processing_progress: progress };
                }
                return att;
              });
            }
          } else if (contentType) {
            if (!expectedDerivationsRef.current.has(artifactId)) {
              initializeTrackingRefs(artifactId, contentType);
            } else {
              const existingExpected = expectedDerivationsRef.current.get(artifactId);
              const existingCompleted = completedStepsRef.current.get(artifactId) || 0;
              const existingTotal = totalStepsRef.current.get(artifactId) || 0;

              console.log("[Create Artifact] Refs already exist (derivation arrived first), preserving:", {
                artifactId, contentType,
                expectedRemaining: existingExpected?.size ?? 0,
                completedSteps: existingCompleted,
                totalSteps: existingTotal
              });

              if (filename && bytes !== undefined) {
                artifactMetadataRef.current.set(artifactId, { content_type: contentType, filename, bytes });
              }
            }
          }
          return prev;
        });
        return;
      }

      // Handle derive_upload_image_completed
      if (eventKind === "core.derive_upload_image_completed" && eventData.status === "success") {
        const sourceArtifactId = getSourceArtifactId(eventData);
        if (!sourceArtifactId) {
          console.warn("[Derive Image] Missing source_artifact_id");
          return;
        }

        setAttachmentList((prev) => {
          const attachment = prev.find(att => att.artifact_id === sourceArtifactId);

          if (!attachment) {
            const cachedMeta = artifactMetadataRef.current.get(sourceArtifactId);
            const contentType = cachedMeta?.content_type || "image/unknown";
            if (!expectedDerivationsRef.current.has(sourceArtifactId)) {
              initializeTrackingRefs(sourceArtifactId, contentType);
            }
            markDerivationComplete(sourceArtifactId, "image", IMAGE_DERIVATION_STEPS);
            return prev;
          }

          const allDone = markDerivationComplete(sourceArtifactId, "image", IMAGE_DERIVATION_STEPS);

          if (allDone) {
            markAttachmentReady(sourceArtifactId);
            return prev.map((att) => {
              if (att.artifact_id === sourceArtifactId) {
                return { ...att, status: "ready" as const, processing_step: undefined, processing_progress: undefined };
              }
              return att;
            });
          }

          const expected = expectedDerivationsRef.current.get(sourceArtifactId);
          const totalSteps = totalStepsRef.current.get(sourceArtifactId) || 0;
          const completedSteps = completedStepsRef.current.get(sourceArtifactId) || 0;
          const progress = totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0;

          return prev.map((att) => {
            if (att.artifact_id === sourceArtifactId && expected) {
              const stepDescription = getProcessingStepDescription(sourceArtifactId, expected, completedSteps, totalSteps, att.content_type);
              return { ...att, processing_step: stepDescription, processing_progress: progress };
            }
            return att;
          });
        });
        return;
      }

      // Handle derive_video_visuals_completed / derive_video_transcript_completed
      // (ADR-0119: the two video tracks derive in parallel and complete independently)
      if (eventKind === "core.derive_video_visuals_completed" || eventKind === "core.derive_video_transcript_completed") {
        const derivationType = eventKind === "core.derive_video_visuals_completed" ? "video_visuals" : "video_transcript";
        const sourceArtifactId = getSourceArtifactId(eventData);
        if (!sourceArtifactId) {
          console.warn("[Derive Video] Missing source_artifact_id", { derivationType });
          return;
        }
        if (eventData.status !== "success") {
          console.warn("[Derive Video] Derivation skipped or failed", {
            sourceArtifactId, derivationType, status: eventData.status,
            reason: eventData.result?.reason || eventData.error?.message
          });
        }

        const clearDerivationRefs = () => {
          expectedDerivationsRef.current.delete(sourceArtifactId);
          totalStepsRef.current.delete(sourceArtifactId);
          completedStepsRef.current.delete(sourceArtifactId);
          const timeoutId = derivationTimeoutsRef.current.get(sourceArtifactId);
          if (timeoutId) {
            clearTimeout(timeoutId);
            derivationTimeoutsRef.current.delete(sourceArtifactId);
          }
        };

        setAttachmentList((prev) => {
          const attachment = prev.find((att) => att.artifact_id === sourceArtifactId);

          if (!attachment) {
            const cachedMeta = artifactMetadataRef.current.get(sourceArtifactId);
            const contentType = cachedMeta?.content_type || "video/unknown";
            if (!expectedDerivationsRef.current.has(sourceArtifactId)) {
              initializeTrackingRefs(sourceArtifactId, contentType);
            }
            markDerivationComplete(sourceArtifactId, derivationType, 1);
            return prev;
          }

          const allDone = markDerivationComplete(sourceArtifactId, derivationType, 1);
          const totalSteps = totalStepsRef.current.get(sourceArtifactId) || 0;
          const completedSteps = completedStepsRef.current.get(sourceArtifactId) || 0;
          const progress = totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : undefined;

          const trackKey = derivationType === "video_visuals" ? "visuals" : "transcript";
          const video_derivation: VideoDerivationStatus = {
            ...(attachment.video_derivation ?? initialVideoDerivationStatus()),
            [trackKey]: videoDerivationTrackFromEventStatus(eventData.status),
          };

          if (allDone) {
            clearDerivationRefs();
            return prev.map((att) => {
              if (att.artifact_id !== sourceArtifactId) return att;
              return {
                ...att,
                video_derivation,
                status: "ready" as const,
                processing_step: undefined,
                processing_progress: undefined,
              };
            });
          }

          return prev.map((att) => {
            if (att.artifact_id !== sourceArtifactId) return att;
            return {
              ...att,
              video_derivation,
              processing_step: formatVideoDerivationProcessingStep(video_derivation),
              processing_progress: progress,
            };
          });
        });
        return;
      }

      // Handle derive_pdf_page_images_completed
      if (eventKind === "core.derive_pdf_page_images_completed") {
        const sourceArtifactId = getSourceArtifactId(eventData);
        if (!sourceArtifactId) {
          console.warn("[PDF Page Images] Missing source_artifact_id");
          return;
        }

        const totalPages = resolvePdfTotalPagesFromEvent(eventData);
        if (totalPages === undefined) {
          console.warn("[PDF Page Images] Missing total_pages in completion event", {
            sourceArtifactId,
            status: eventData.status,
            keys: Object.keys(eventData),
          });
          return;
        }

        const pdfMeta = pdfMetadataRef.current.get(sourceArtifactId) || { ocrPagesCompleted: 0 };
        pdfMeta.totalPages = totalPages;
        pdfMetadataRef.current.set(sourceArtifactId, pdfMeta);

        const newTotalSteps = 1 + totalPages + 1;
        totalStepsRef.current.set(sourceArtifactId, newTotalSteps);

        const currentCompleted = completedStepsRef.current.get(sourceArtifactId) || 0;
        const newCompleted = Math.min(currentCompleted + 1, newTotalSteps);
        completedStepsRef.current.set(sourceArtifactId, newCompleted);

        const progress = Math.round((newCompleted / newTotalSteps) * 100);
        const expected = expectedDerivationsRef.current.get(sourceArtifactId);
        if (expected) {
          setAttachmentList((prev) => {
            return prev.map((att) => {
              if (att.artifact_id === sourceArtifactId) {
                const stepDescription = getProcessingStepDescription(
                  sourceArtifactId,
                  expected,
                  newCompleted,
                  newTotalSteps,
                  att.content_type
                );
                return { ...att, processing_step: stepDescription, processing_progress: progress };
              }
              return att;
            });
          });
        }
        return;
      }

      // Handle ocr_image_page_completed
      if (eventKind === "core.ocr_image_page_completed") {
        const sourceArtifactId = getSourceArtifactId(eventData);
        if (!sourceArtifactId) {
          console.warn("[OCR Page] Missing source_artifact_id");
          return;
        }

        const pageNum = resolveOcrPageNumFromEvent(eventData);
        const pdfMeta = pdfMetadataRef.current.get(sourceArtifactId) || { ocrPagesCompleted: 0 };
        if (pageNum !== undefined) {
          if (!pdfMeta.totalPages || pageNum > pdfMeta.totalPages) {
            pdfMeta.totalPages = pageNum;
          }
          pdfMeta.ocrPagesCompleted = Math.max(pdfMeta.ocrPagesCompleted || 0, pageNum);
        } else {
          pdfMeta.ocrPagesCompleted = (pdfMeta.ocrPagesCompleted || 0) + 1;
        }
        pdfMetadataRef.current.set(sourceArtifactId, pdfMeta);

        const totalSteps = totalStepsRef.current.get(sourceArtifactId) || 0;
        let newCompleted = completedStepsRef.current.get(sourceArtifactId) || 0;
        if (pdfMeta.totalPages && totalSteps > 0) {
          // Align step counter with page-image + OCR pipeline when we know page count.
          newCompleted = Math.min(1 + pdfMeta.ocrPagesCompleted, totalSteps);
        } else {
          newCompleted = newCompleted + 1;
        }
        completedStepsRef.current.set(sourceArtifactId, newCompleted);

        const progress = totalSteps > 0 ? Math.round((newCompleted / totalSteps) * 100) : 0;
        const expected = expectedDerivationsRef.current.get(sourceArtifactId);

        if (expected) {
          setAttachmentList((prev) => {
            return prev.map((att) => {
              if (att.artifact_id === sourceArtifactId && att.status === "processing") {
                const stepDescription = getProcessingStepDescription(
                  sourceArtifactId,
                  expected,
                  newCompleted,
                  totalSteps,
                  att.content_type
                );
                return { ...att, processing_step: stepDescription, processing_progress: progress };
              }
              return att;
            });
          });
        }
        return;
      }

      // Handle derive_upload_text_completed
      if (eventKind === "core.derive_upload_text_completed") {
        const sourceArtifactId = getSourceArtifactId(eventData);
        console.log("[Derive Text] Processing event:", {
          sourceArtifactId,
          status: eventData.status,
          hasResult: !!eventData.result,
          eventDataStructure: {
            source_artifact_id: eventData.source_artifact_id,
            result_source_artifact_id: eventData.result?.source_artifact_id,
            result_data_source_artifact_id: eventData.result?.data?.source_artifact_id,
            command_data_source_artifact_id: eventData.command_data?.source_artifact_id
          }
        });

        if (!sourceArtifactId) {
          console.warn("[Derive Text] Missing source_artifact_id - cannot process", {
            eventDataKeys: Object.keys(eventData),
            resultKeys: eventData.result ? Object.keys(eventData.result) : null,
            resultDataKeys: eventData.result?.data ? Object.keys(eventData.result.data) : null
          });
          const fallbackId = getArtifactId(eventData) || eventData.artifact_id;
          if (fallbackId) {
            console.warn("[Derive Text] Using fallback artifact_id", { fallbackId });
            markDerivationComplete(fallbackId, "text", 2);
            markAttachmentReady(fallbackId);
            setAttachmentList((prev) => {
              return prev.map((att) => {
                if (att.artifact_id === fallbackId) {
                  return { ...att, status: "ready" as const, processing_step: undefined, processing_progress: undefined };
                }
                return att;
              });
            });
          }
          return;
        }

        if (eventData.status === "success") {
          setAttachmentList((prev) => {
            const attachment = prev.find(att => att.artifact_id === sourceArtifactId);

            if (!attachment) {
              const cachedMeta = artifactMetadataRef.current.get(sourceArtifactId);
              const contentType = cachedMeta?.content_type || "text/plain";

              if (!expectedDerivationsRef.current.has(sourceArtifactId)) {
                const derivations = new Set(["text"]);
                expectedDerivationsRef.current.set(sourceArtifactId, derivations);
                totalStepsRef.current.set(sourceArtifactId, 2);
                completedStepsRef.current.set(sourceArtifactId, 1);
              }

              const allDone = markDerivationComplete(sourceArtifactId, "text", 1);
              console.log("[Derive Text] Attachment not in list yet, initialized and updated refs:", {
                sourceArtifactId, allDone,
                expectedRemaining: expectedDerivationsRef.current.get(sourceArtifactId)?.size ?? 0,
                completedSteps: completedStepsRef.current.get(sourceArtifactId),
                contentType
              });
              return prev;
            }

            const stepsToComplete = 1;
            const allDone = markDerivationComplete(sourceArtifactId, "text", stepsToComplete);

            if (allDone) {
              markAttachmentReady(sourceArtifactId);
              return prev.map((att) => {
                if (att.artifact_id === sourceArtifactId) {
                  return { ...att, status: "ready" as const, processing_step: undefined, processing_progress: undefined };
                }
                return att;
              });
            } else {
              const expected = expectedDerivationsRef.current.get(sourceArtifactId);
              const totalSteps = totalStepsRef.current.get(sourceArtifactId) || 0;
              const completedSteps = completedStepsRef.current.get(sourceArtifactId) || 0;
              const progress = totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0;

              return prev.map((att) => {
                if (att.artifact_id === sourceArtifactId && expected) {
                  const stepDescription = getProcessingStepDescription(sourceArtifactId, expected, completedSteps, totalSteps, att.content_type);
                  return { ...att, processing_step: stepDescription, processing_progress: progress };
                }
                return att;
              });
            }
          });
          return;
        }

        // Handle skipped/error/unknown cases
        if (eventData.status === "skipped" || eventData.status === "error") {
          console.warn("[Derive Text] Derivation skipped or failed", {
            sourceArtifactId, status: eventData.status,
            reason: eventData.result?.reason || eventData.error?.message
          });
        } else {
          console.warn("[Derive Text] Unknown status", { sourceArtifactId, status: eventData.status });
        }
        if (!expectedDerivationsRef.current.has(sourceArtifactId)) {
          const derivations = new Set(["text"]);
          expectedDerivationsRef.current.set(sourceArtifactId, derivations);
          totalStepsRef.current.set(sourceArtifactId, 2);
          completedStepsRef.current.set(sourceArtifactId, 0);
        }
        markDerivationComplete(sourceArtifactId, "text", 2);
        markAttachmentReady(sourceArtifactId);
        setAttachmentList((prev) => {
          return prev.map((att) => {
            if (att.artifact_id === sourceArtifactId) {
              return { ...att, status: "ready" as const, processing_step: undefined, processing_progress: undefined };
            }
            return att;
          });
        });
        return;
      }
    };

    connect();

    return () => {
      isMounted = false;
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      if (eventsAbortController) eventsAbortController.abort();
    };
  }, [auth, getExpectedDerivations, getTotalStepsForDerivation, getProcessingStepDescription, getSourceArtifactId, getArtifactId, initializeTrackingRefs, markDerivationComplete, markAttachmentReady, calculateTotalSteps, resolvePdfTotalPagesFromEvent, resolveOcrPageNumFromEvent]);

  // ─────────────────────────────────────────────────────────────────────────────
  // CALLBACKS: Blob URL and artifact management
  // ─────────────────────────────────────────────────────────────────────────────

  const removeBlobUrl = useCallback((artifactId: string) => {
    const url = imageBlobUrlsRef.current.get(artifactId);
    if (url) URL.revokeObjectURL(url);
    setImageBlobUrls((prev) => {
      if (!prev.has(artifactId)) return prev;
      const next = new Map(prev);
      next.delete(artifactId);
      return next;
    });
  }, []);

  const ensureImagePreview = useCallback(async (artifactId: string): Promise<string | undefined> => {
    if (imageBlobUrls.has(artifactId)) return imageBlobUrls.get(artifactId);

    const existingPromise = fetchPromiseRef.current.get(artifactId);
    if (existingPromise) return existingPromise;

    const fetchPromise = (async () => {
      try {
        const headers = buildAuthHeaders(auth);
        const response = await fetch(`/api/v1/artifacts/${artifactId}/preview`, { headers });
        if (!response.ok) return undefined;
        const blob = await response.blob();
        const blobUrl = URL.createObjectURL(blob);
        setImageBlobUrls((prev) => {
          const next = new Map(prev);
          const existing = next.get(artifactId);
          if (existing) URL.revokeObjectURL(existing);
          next.set(artifactId, blobUrl);
          return next;
        });
        return blobUrl;
      } catch {
        return undefined;
      } finally {
        fetchPromiseRef.current.delete(artifactId);
      }
    })();

    fetchPromiseRef.current.set(artifactId, fetchPromise);
    return fetchPromise;
  }, [auth, imageBlobUrls]);

  /**
   * Ensure a streamable playback URL exists for a video artifact (ADR-0118
   * Phase A.2). Mints a short-lived token via POST /playback-token and caches
   * the resulting /stream URL; re-mints when the token nears expiry. Unlike
   * image previews this is NOT a blob URL — the <video> element hits the
   * stream endpoint directly so HTTP Range seek/scrub works.
   */
  const ensureVideoSource = useCallback(async (artifactId: string): Promise<string | undefined> => {
    const expiresAt = videoStreamExpiryRef.current.get(artifactId) || 0;
    const cached = videoStreamUrls.get(artifactId);
    // Re-mint when within 30s of expiry so an in-flight playback doesn't 401.
    if (cached && Date.now() < expiresAt - 30_000) return cached;

    const existingPromise = videoFetchPromiseRef.current.get(artifactId);
    if (existingPromise) return existingPromise;

    const fetchPromise = (async () => {
      try {
        const headers = buildAuthHeaders(auth);
        const response = await fetch(`/api/v1/artifacts/${artifactId}/playback-token`, {
          method: "POST",
          headers
        });
        if (!response.ok) return undefined;
        const data = await response.json();
        const streamUrl: string | undefined = data?.stream_url;
        if (!streamUrl) return undefined;
        const expiresInMs = (Number(data?.expires_in) || 300) * 1000;
        videoStreamExpiryRef.current.set(artifactId, Date.now() + expiresInMs);
        setVideoStreamUrls((prev) => {
          const next = new Map(prev);
          next.set(artifactId, streamUrl);
          return next;
        });
        return streamUrl;
      } catch {
        return undefined;
      } finally {
        videoFetchPromiseRef.current.delete(artifactId);
      }
    })();

    videoFetchPromiseRef.current.set(artifactId, fetchPromise);
    return fetchPromise;
  }, [auth, videoStreamUrls]);

  const deleteArtifactBestEffort = useCallback(async (artifactId: string): Promise<void> => {
    try {
      const headers = buildAuthHeaders(auth);
      const res = await fetch(`/api/v1/artifacts/${artifactId}`, { method: "DELETE", headers });
      if (!res.ok && res.status !== 404) {
        const body = await res.text().catch(() => "");
        console.warn("artifact_delete_failed", artifactId, res.status, body);
      }
    } catch (e) {
      console.warn("artifact_delete_error", artifactId, e);
    }
  }, [auth]);

  useEffect(() => {
    return () => {
      for (const url of imageBlobUrlsRef.current.values()) URL.revokeObjectURL(url);
      for (const timeoutId of derivationTimeoutsRef.current.values()) clearTimeout(timeoutId);
      derivationTimeoutsRef.current.clear();
      expectedDerivationsRef.current.clear();
      totalStepsRef.current.clear();
      completedStepsRef.current.clear();
      pdfMetadataRef.current.clear();
      artifactMetadataRef.current.clear();
    };
  }, []);

  const handleUpload = async (file: File): Promise<{
    artifact_id: string;
    filename: string;
    content_type: string;
    bytes: number;
    derivations_pending: boolean;
  }> => {
    const formData = new FormData();
    formData.append("file", file);

    const url = new URL("/api/v1/artifacts", window.location.origin);
    if (conversationId) url.searchParams.set("conversation_id", conversationId);
    const provider = String(overrides?.model_provider || "").trim();
    const model = String(overrides?.model_name || "").trim();
    const modelProfile = String(overrides?.model_profile_name || "").trim();
    if (provider) url.searchParams.set("model_provider", provider);
    if (model) url.searchParams.set("model_name", model);
    if (modelProfile) url.searchParams.set("model_profile_name", modelProfile);

    const uploadHeaders = buildAuthHeaders(auth);
    const response = await fetch(url.toString(), {
      method: "POST",
      body: formData,
      headers: uploadHeaders
    });

    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`Upload failed: ${response.status} ${response.statusText}${body ? ` - ${body}` : ""}`);
    }

    const data = await response.json();

    const existingExpected = expectedDerivationsRef.current.get(data.artifact_id);
    const existingTotalSteps = totalStepsRef.current.get(data.artifact_id);
    const existingCompleted = completedStepsRef.current.get(data.artifact_id);

    const expectedDerivations = existingExpected ?? getExpectedDerivations(data.content_type);
    const allDone = existingExpected !== undefined && existingExpected.size === 0;
    const hasNoDerivations = expectedDerivations.size === 0;
    const initialStatus = allDone || hasNoDerivations ? "ready" : (expectedDerivations.size > 0 ? "processing" : "ready");

    if (allDone) {
      expectedDerivationsRef.current.delete(data.artifact_id);
      totalStepsRef.current.delete(data.artifact_id);
      completedStepsRef.current.delete(data.artifact_id);
      artifactMetadataRef.current.delete(data.artifact_id);
    }

    const derivationsPending = expectedDerivations.size > 0 && !allDone;
    const isVideo = String(data.content_type || "").startsWith("video/");
    const initialVideoDerivation = isVideo ? initialVideoDerivationStatus() : undefined;

    let initialProcessingStep: string | undefined = undefined;
    let initialProcessingProgress: number | undefined = undefined;

    if (derivationsPending) {
      const totalSteps = existingTotalSteps ?? calculateTotalSteps(expectedDerivations, data.content_type);
      const completedSteps = existingCompleted ?? 0;
      initialProcessingStep =
        initialVideoDerivation
          ? formatVideoDerivationProcessingStep(initialVideoDerivation)
          : getProcessingStepDescription(
              data.artifact_id,
              expectedDerivations,
              completedSteps,
              totalSteps,
              data.content_type
            );
      initialProcessingProgress = totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0;
      if (!existingExpected) {
        initializeTrackingRefs(data.artifact_id, data.content_type, expectedDerivations);
      }
    }

    const newAttachment: AttachmentState = {
      artifact_id: data.artifact_id,
      filename: data.filename,
      content_type: data.content_type,
      bytes: data.bytes,
      status: initialStatus,
      processing_step: initialProcessingStep,
      processing_progress: initialProcessingProgress,
      video_derivation: initialVideoDerivation,
    };
    setAttachmentList((prev) => [...prev, newAttachment]);

    if (derivationsPending) {
      const timeoutMs = getDerivationTimeoutMs(data.content_type);
      const timeoutId = setTimeout(() => {
        console.warn("[Derivation Timeout] Marking attachment as ready after timeout", {
          artifactId: data.artifact_id, contentType: data.content_type,
          expectedDerivations: Array.from(expectedDerivations), timeoutMs
        });
        setAttachmentList((prev) => {
          return prev.map((att) => {
            if (att.artifact_id === data.artifact_id && att.status === "processing") {
              return { ...att, status: "ready" as const, processing_step: undefined, processing_progress: undefined };
            }
            return att;
          });
        });
        expectedDerivationsRef.current.delete(data.artifact_id);
        totalStepsRef.current.delete(data.artifact_id);
        completedStepsRef.current.delete(data.artifact_id);
        pdfMetadataRef.current.delete(data.artifact_id);
        derivationTimeoutsRef.current.delete(data.artifact_id);
      }, timeoutMs);
      derivationTimeoutsRef.current.set(data.artifact_id, timeoutId);
    }

    if (String(data.content_type || "").startsWith("image/")) {
      void ensureImagePreview(data.artifact_id);
    }
    if (String(data.content_type || "").startsWith("video/")) {
      void ensureVideoSource(data.artifact_id);
    }

    return {
      artifact_id: data.artifact_id,
      filename: data.filename,
      content_type: data.content_type,
      bytes: data.bytes,
      derivations_pending: derivationsPending,
    };
  };

  // ─────────────────────────────────────────────────────────────────────────────
  // DERIVED STATE: Combine drafts and completed uploads for UI
  // ─────────────────────────────────────────────────────────────────────────────

  const fileCardList = useMemo(() => {
    const draftItems = draftUploads.map((d) => ({
      uid: d.uid,
      name: d.name,
      size: d.size,
      type: d.type,
      status: d.status as "error" | "done" | "uploading" | "removed",
      percent: d.percent
    }));

    const persistedItems = attachmentList.map(att => {
      let status: "error" | "done" | "uploading" | "removed";
      if (att.status === "error") {
        status = "error";
      } else if (att.status === "processing") {
        status = "uploading";
      } else if (att.content_type.startsWith("image/") && !imageBlobUrls.has(att.artifact_id)) {
        status = "uploading";
      } else if (att.status === "ready") {
        status = "done";
      } else {
        status = "done";
      }

      const cardProps = inferFileCardProps(att, {
        imageSrc: att.content_type.startsWith("image/") ? imageBlobUrls.get(att.artifact_id) : undefined
      });

      const fileCardItem: any = {
        uid: att.artifact_id,
        name: att.filename,
        size: att.bytes,
        type: att.content_type,
        status: status as "error" | "done" | "uploading" | "removed",
        url: att.content_type.startsWith("image/") ? imageBlobUrls.get(att.artifact_id) : undefined,
      };
      if (cardProps.description) fileCardItem.description = cardProps.description;
      if (cardProps.percent !== undefined) fileCardItem.percent = cardProps.percent;
      return fileCardItem;
    });

    return [...draftItems, ...persistedItems];
  }, [attachmentList, draftUploads, imageBlobUrls]);

  return {
    attachmentList,
    setAttachmentList,
    imageBlobUrls,
    setImageBlobUrls,
    showAttachments,
    setShowAttachments,
    draftUploads,
    setDraftUploads,
    handleUpload,
    ensureImagePreview,
    videoStreamUrls,
    ensureVideoSource,
    deleteArtifactBestEffort,
    removeBlobUrl,
    fileCardList
  };
}
