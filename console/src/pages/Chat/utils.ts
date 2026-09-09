// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
import { getApiToken } from "../../api/config";
import { chatApi } from "../../api/modules/chat";
export type CopyableContent = {
  type?: string;
  text?: string;
  refusal?: string;
};

export type CopyableMessage = {
  role?: string;
  content?: string | CopyableContent[];
};

export type CopyableResponse = {
  output?: CopyableMessage[];
};

export type RuntimeLoadingBridgeApi = {
  getLoading?: () => boolean | string;
  setLoading?: (loading: boolean | string) => void;
};

// ---------------------------------------------------------------------------
// Text extraction utilities
// ---------------------------------------------------------------------------

/** Extract copyable text from assistant response. */
export function extractCopyableText(response: CopyableResponse): string {
  const collectText = (assistantOnly: boolean) => {
    const chunks = (response.output || []).flatMap((item: CopyableMessage) => {
      if (assistantOnly && item.role !== "assistant") return [];

      if (typeof item.content === "string") {
        return [item.content];
      }

      if (!Array.isArray(item.content)) {
        return [];
      }

      return item.content.flatMap((content: CopyableContent) => {
        if (content.type === "text" && typeof content.text === "string") {
          return [content.text];
        }

        if (content.type === "refusal" && typeof content.refusal === "string") {
          return [content.refusal];
        }

        return [];
      });
    });

    return chunks.filter(Boolean).join("\n\n").trim();
  };

  return collectText(true) || JSON.stringify(response);
}

/** Extract plain text from user message content. */
export function extractUserMessageText(m: any): string {
  if (typeof m.content === "string") return m.content;
  if (!Array.isArray(m.content)) return "";
  return m.content
    .filter((p: any) => p.type === "text")
    .map((p: any) => p.text || "")
    .join("\n");
}

export function extractTextFromMessage(msg: any): string {
  const innerMessage = msg?.cards?.[0]?.data?.input?.[0];
  if (!innerMessage) return "";
  return extractUserMessageText(innerMessage);
}

// ---------------------------------------------------------------------------
// Clipboard utilities
// ---------------------------------------------------------------------------

/** Copy text to clipboard with fallback for non-secure contexts. */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "absolute";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);

  let copied = false;
  try {
    textarea.focus();
    textarea.select();
    copied = document.execCommand("copy");
  } finally {
    document.body.removeChild(textarea);
  }

  if (!copied) {
    throw new Error("Failed to copy text");
  }
}

// ---------------------------------------------------------------------------
// Error response utilities
// ---------------------------------------------------------------------------

/** Build a 400 error response when model is not configured. */
export function buildModelError(): Response {
  return new Response(
    JSON.stringify({
      error: "Model not configured",
      message: "Please configure a model first",
    }),
    { status: 400, headers: { "Content-Type": "application/json" } },
  );
}

// ---------------------------------------------------------------------------
// URL normalization utilities
// ---------------------------------------------------------------------------

/**
 * Absolute URL whose path is under the LCAgent upload area
 * (``/static/upload/...`` or ``/app/upload/...``) → same-origin relative
 * ``/static/upload/...``; returns null for anything else.
 *
 * Idempotent: also accepts an already-relative ``/static/upload/...`` (returned
 * as-is) and ``/app/upload/...``. ``toDisplayUrl`` runs twice on the same value
 * (once when synthesizing image blocks, once via ``replaceMediaURL``), so a
 * non-idempotent rewrite would send the 2nd pass down the copaw preview path
 * and 404.
 */
function lcagentUploadUrlToStaticRelative(raw: string): string | null {
  const t = raw.trim();
  // Already-relative upload paths: normalize to /static/upload/ and stop.
  if (t.startsWith("/static/upload/")) return t;
  if (t.startsWith("/app/upload/"))
    return `/static/upload/${t.slice("/app/upload/".length)}`;
  if (!t.startsWith("http://") && !t.startsWith("https://")) return null;
  try {
    const u = new URL(t);
    const path = u.pathname || "";
    const staticIdx = path.indexOf("/static/upload/");
    if (staticIdx !== -1) return path.slice(staticIdx);
    const uploadIdx = path.indexOf("/app/upload/");
    if (uploadIdx !== -1)
      return `/static/upload/${path.slice(uploadIdx + "/app/upload/".length)}`;
  } catch {
    return null;
  }
  return null;
}

/** 与 LCAgent 首页 Markdown 一致：嵌入小控制台时把绝对下载链收成相对路径，沿用当前页协议。 */
function absolutizeConsoleDownloadToRelativeWhenEmbedded(raw: string): string {
  if (typeof window === "undefined") return raw;
  const pathname = window.location.pathname;
  const embedded =
    /^\/copaw(?:\/|$)/.test(pathname) || /^\/console(?:\/|$)/.test(pathname);
  if (!embedded) return raw;
  const t = raw.trim();
  if (!t.startsWith("http://") && !t.startsWith("https://")) return raw;
  if (!t.includes("/files/download")) return raw;
  try {
    const u = new URL(t);
    if (!u.pathname.includes("/files/download")) return raw;
    let fp = u.searchParams.get("file_path");
    if (fp == null || fp === "") {
      const legacy = u.searchParams.get("path");
      if (legacy != null && legacy !== "") fp = legacy;
    }
    if (fp == null || fp === "") return raw;
    return `/console/api/files/download?file_path=${encodeURIComponent(fp)}`;
  } catch {
    return raw;
  }
}

/** True when this is a same-origin LCAgent Flask download path (not lcClaw /copaw preview). */
function isLcagentConsoleFilesDownloadRef(relativePathWithQuery: string): boolean {
  const t = relativePathWithQuery.trim();
  if (t.startsWith("http://") || t.startsWith("https://")) return false;
  const noHash = t.split("#")[0] ?? "";
  const q = noHash.indexOf("?");
  const pathOnly = (q === -1 ? noHash : noHash.slice(0, q)).trim();
  const p = pathOnly.startsWith("/") ? pathOnly : `/${pathOnly}`;
  return p.includes("/console/api/files/download");
}

function isCopawPreviewRef(pathOrUrl: string): boolean {
  const t = pathOrUrl.trim();
  if (!t) return false;
  try {
    const u = new URL(
      t,
      typeof window !== "undefined" ? window.location.origin : "http://localhost",
    );
    return (
      u.pathname.includes("/copaw/api/files/preview/") ||
      u.pathname.includes("/api/files/preview/")
    );
  } catch {
    return (
      t.includes("/copaw/api/files/preview/") || t.includes("/api/files/preview/")
    );
  }
}

function appendPreviewTokenIfMissing(url: string): string {
  const token = getApiToken();
  if (!token || url.includes("token=")) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

/** Same-origin absolute URL for LCAgent download; never pass through filePreviewUrl. */
function lcagentConsoleDownloadDisplayUrl(pathWithOptionalQuery: string): string {
  const noHash = pathWithOptionalQuery.split("#")[0] ?? "";
  const normalized = noHash.startsWith("/") ? noHash : `/${noHash}`;
  const relative = normalized;
  const withToken = appendPreviewTokenIfMissing(relative);
  if (typeof window !== "undefined") {
    return `${window.location.origin}${withToken}`;
  }
  return withToken;
}

function copawPreviewDisplayUrl(pathOrUrl: string): string {
  const withToken = appendPreviewTokenIfMissing(pathOrUrl);
  if (
    withToken.startsWith("http://") ||
    withToken.startsWith("https://") ||
    typeof window === "undefined"
  ) {
    return withToken;
  }
  const normalized = withToken.startsWith("/") ? withToken : `/${withToken}`;
  return `${window.location.origin}${normalized}`;
}

/** Decode each path segment; keeps `/` delimiters (including repeated `/`). */
function decodeUriPathSegments(path: string): string {
  return path
    .split("/")
    .map((segment) => {
      if (!segment) return segment;
      try {
        return decodeURIComponent(segment);
      } catch {
        return segment;
      }
    })
    .join("/");
}

/** Convert file URL to stored path for backend: keep full path after `/files/preview/`. */
export function toStoredName(v: string): string {
  for (const copawMarker of ["/copaw/api/files/preview/", "/api/files/preview/"]) {
    const copawIdx = v.indexOf(copawMarker);
    if (copawIdx !== -1) {
      let rest = v.slice(copawIdx + copawMarker.length);
      const q = rest.indexOf("?");
      if (q !== -1) rest = rest.slice(0, q);
      const h = rest.indexOf("#");
      if (h !== -1) rest = rest.slice(0, h);
      if (rest) {
        const decoded = decodeUriPathSegments(rest).replace(/^\/+/, "");
        return `/app/working/users/${decoded}`;
      }
    }
  }
  const marker = "/files/preview/";
  const idx = v.indexOf(marker);
  if (idx !== -1) {
    let rest = v.slice(idx + marker.length);
    const q = rest.indexOf("?");
    if (q !== -1) rest = rest.slice(0, q);
    const h = rest.indexOf("#");
    if (h !== -1) rest = rest.slice(0, h);
    if (rest) {
      const decoded = decodeUriPathSegments(rest);
      // Windows absolute path: C:\... or C:/...
      const isWindowsAbsolute = /^[a-zA-Z]:[\\/]/.test(decoded);
      if (isWindowsAbsolute) return decoded;
      return decoded.startsWith("/") ? decoded : `/${decoded}`;
    }
  }
  return v;
}

/** Convert content part URLs to stored name format. */
export function normalizeContentUrls(part: any): any {
  const p = { ...part };
  if (p.type === "image" && typeof p.image_url === "string")
    p.image_url = toStoredName(p.image_url);
  if (p.type === "file" && typeof p.file_url === "string")
    p.file_url = toStoredName(p.file_url);
  if (p.type === "audio" && typeof p.data === "string")
    p.data = toStoredName(p.data);
  if (p.type === "video" && typeof p.video_url === "string")
    p.video_url = toStoredName(p.video_url);
  return p;
}

/** Turn a backend content URL (path or full URL) into a full URL for display. */
export function toDisplayUrl(url: string | undefined): string {
  if (!url) return "";
  if (url.startsWith("http://") || url.startsWith("https://")) {
    // send_file may embed a host unreachable from this browser (e.g.
    // http://127.0.0.1:30382/static/upload/...). Upload-area URLs are served
    // by the same nginx as this page, so rewrite to a same-origin relative
    // path; other hosts (external links) stay untouched.
    const staticRel = lcagentUploadUrlToStaticRelative(url);
    if (staticRel) return staticRel;
    const relOrRaw = absolutizeConsoleDownloadToRelativeWhenEmbedded(url);
    if (isCopawPreviewRef(relOrRaw)) {
      return copawPreviewDisplayUrl(relOrRaw);
    }
    if (
      relOrRaw.startsWith("/") &&
      isLcagentConsoleFilesDownloadRef(relOrRaw)
    ) {
      return lcagentConsoleDownloadDisplayUrl(relOrRaw);
    }
    return relOrRaw;
  }
  if (url.startsWith("file://")) url = url.replace("file://", "");
  // Already-relative upload path (2nd pass of a value we rewrote): keep as-is,
  // do NOT route through copaw preview (which would 404 on /static/upload/...).
  const uploadRel = lcagentUploadUrlToStaticRelative(url);
  if (uploadRel) return uploadRel;
  const normalized = url.startsWith("/") ? url : `/${url}`;
  if (isCopawPreviewRef(normalized)) {
    return copawPreviewDisplayUrl(normalized);
  }
  if (isLcagentConsoleFilesDownloadRef(normalized)) {
    return lcagentConsoleDownloadDisplayUrl(normalized);
  }
  return chatApi.filePreviewUrl(normalized);
}

/** Markdown images `![alt](url)` — raw URL substrings (for send_file text fallback). */
export function extractMarkdownImageUrls(text: string): string[] {
  const re = /!\[[^\]]*\]\(([^)\s]+)\)/g;
  const urls: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const u = m[1].trim();
    if (u) urls.push(u);
  }
  return urls;
}

/** Remove `![...](...)` spans so structured image blocks do not duplicate rendering. */
export function stripMarkdownImageSyntax(text: string): string {
  return text.replace(/!\[[^\]]*\]\([^)]+\)\s*/g, "").trimEnd();
}

function urlFromImageLikeBlock(block: Record<string, unknown>): string {
  if (typeof block.image_url === "string" && block.image_url.trim())
    return block.image_url.trim();
  const src = block.source;
  if (
    src &&
    typeof src === "object" &&
    String((src as { type?: string }).type || "").toLowerCase() === "url"
  ) {
    const u = (src as { url?: string }).url;
    if (typeof u === "string" && u.trim()) return u.trim();
  }
  return "";
}

/**
 * Parse tool result `data.output` (string or JSON array of content blocks):
 * collect image URLs (markdown in text + structured image blocks) and optionally
 * rewrite output so thumbnails render from `image` parts without duplicate markdown.
 */
export function collectToolOutputImagesAndRewriteOutput(output: unknown): {
  imageUrls: string[];
  displayOutput?: string;
} {
  const imageUrls: string[] = [];

  const addUrlsFromText = (text: string): string => {
    const urls = extractMarkdownImageUrls(text);
    if (urls.length) imageUrls.push(...urls);
    return stripMarkdownImageSyntax(text);
  };

  if (output == null) return { imageUrls: [] };

  if (typeof output === "string") {
    const t = output.trim();
    if (t.startsWith("[") || t.startsWith("{")) {
      try {
        const parsed: unknown = JSON.parse(output);
        if (Array.isArray(parsed)) {
          const next: unknown[] = [];
          let changed = false;
          for (const el of parsed) {
            if (!el || typeof el !== "object") {
              next.push(el);
              continue;
            }
            const b = el as Record<string, unknown>;
            const typ = String(b.type || "");
            if (typ === "text" && typeof b.text === "string") {
              const newText = addUrlsFromText(b.text);
              if (newText !== b.text) changed = true;
              next.push({ ...b, text: newText });
            } else if (typ === "image") {
              const u = urlFromImageLikeBlock(b);
              if (u) {
                imageUrls.push(u);
                changed = true;
              } else next.push(b);
            } else {
              next.push(b);
            }
          }
          const deduped = [...new Set(imageUrls)];
          if (changed || deduped.length > 0) {
            return {
              imageUrls: deduped,
              displayOutput: JSON.stringify(next),
            };
          }
          return { imageUrls: deduped };
        }
      } catch {
        /* treat as plain string */
      }
    }
    const urls = extractMarkdownImageUrls(output);
    if (urls.length === 0) return { imageUrls: [] };
    return {
      imageUrls: [...new Set(urls)],
      displayOutput: stripMarkdownImageSyntax(output),
    };
  }

  if (Array.isArray(output)) {
    const next: unknown[] = [];
    let changed = false;
    for (const el of output) {
      if (!el || typeof el !== "object") {
        next.push(el);
        continue;
      }
      const b = el as Record<string, unknown>;
      const typ = String(b.type || "");
      if (typ === "text" && typeof b.text === "string") {
        const newText = addUrlsFromText(b.text);
        if (newText !== b.text) changed = true;
        next.push({ ...b, text: newText });
      } else if (typ === "image") {
        const u = urlFromImageLikeBlock(b);
        if (u) {
          imageUrls.push(u);
          changed = true;
        } else next.push(b);
      } else {
        next.push(b);
      }
    }
    const deduped = [...new Set(imageUrls)];
    if (changed || deduped.length > 0) {
      return { imageUrls: deduped, displayOutput: JSON.stringify(next) };
    }
    return { imageUrls: deduped };
  }

  return { imageUrls: [] };
}

const STATUS_COMPLETED = "completed";

/** Append synthetic `image` items from `type: data` tool outputs (stream + history). */
export function augmentContentArrayWithToolOutputMedia(
  content: unknown[],
): unknown[] {
  let changed = false;
  const newContent: unknown[] = [];
  const extraImages: Record<string, unknown>[] = [];

  for (const raw of content) {
    if (!raw || typeof raw !== "object") {
      newContent.push(raw);
      continue;
    }
    const item = raw as Record<string, unknown>;
    if (item.type === "data" && item.data && typeof item.data === "object") {
      const data = item.data as Record<string, unknown>;
      if ("output" in data) {
        const { imageUrls, displayOutput } =
          collectToolOutputImagesAndRewriteOutput(data.output);
        if (imageUrls.length > 0 || displayOutput !== undefined) {
          changed = true;
          const newData = {
            ...data,
            ...(displayOutput !== undefined ? { output: displayOutput } : {}),
          };
          newContent.push({ ...item, data: newData });
          for (const u of imageUrls) {
            extraImages.push({
              type: "image",
              image_url: toDisplayUrl(u),
              object: "content",
              delta: false,
              status: STATUS_COMPLETED,
            });
          }
          continue;
        }
      }
    }
    newContent.push(item);
  }

  if (extraImages.length === 0 && !changed) return content;

  const seen = new Set<string>();
  const uniqueExtras = extraImages.filter((im) => {
    const u = String((im as { image_url?: string }).image_url || "");
    if (!u || seen.has(u)) return false;
    seen.add(u);
    return true;
  });

  return [...newContent, ...uniqueExtras];
}

// ---------------------------------------------------------------------------
// DOM utilities
// ---------------------------------------------------------------------------

/** Set textarea value and trigger input event for React state sync.
 * Uses native value setter to bypass React's internal value tracker.
 */
export function setTextareaValue(textarea: HTMLTextAreaElement, value: string) {
  const nativeValueSetter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  if (nativeValueSetter) {
    nativeValueSetter.call(textarea, value);
  } else {
    textarea.value = value;
  }
  textarea.selectionStart = textarea.selectionEnd = value.length;
  const event = new Event("input", { bubbles: true });
  textarea.dispatchEvent(event);
}
