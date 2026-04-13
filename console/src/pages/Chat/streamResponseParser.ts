/**
 * CoPaw SSE chunks: after text deltas, a final `object: message` may carry only
 * file/image/audio/video blocks. @agentscope-ai/chat can drop those; mirror
 * front/app/(appLayout)/home/copawSse.ts by appending markdown text so the
 * stream merge path still shows attachments without refresh.
 */

import {
  augmentContentArrayWithToolOutputMedia,
  extractMarkdownImageUrls,
} from "./utils";

export type StreamDeltaState = { hasStreamedDelta: boolean };

const STATUS_COMPLETED = "completed";

function urlFromSource(item: Record<string, unknown>): string {
  const src = item.source;
  if (!src || typeof src !== "object") return "";
  if (String((src as { type?: string }).type || "").toLowerCase() !== "url")
    return "";
  return String((src as { url?: string }).url || "").trim();
}

/** Same idea as copawSse.attachmentsAppendixFromMessageContent; extend audio/video. */
function attachmentsAppendixFromContent(content: unknown[]): string {
  if (!Array.isArray(content)) return "";
  const lines: string[] = [];
  for (const item of content) {
    if (!item || typeof item !== "object") continue;
    const rec = item as Record<string, unknown>;
    const t = rec.type;
    if (t === "file") {
      let u =
        typeof rec.file_url === "string" ? rec.file_url.trim() : "";
      if (!u) u = urlFromSource(rec);
      if (u) {
        const name = String(rec.filename || rec.file_name || "file").trim();
        lines.push(name && name !== "file" ? `[${name}](${u})` : u);
      }
    } else if (t === "image") {
      let u =
        typeof rec.image_url === "string" ? rec.image_url.trim() : "";
      if (!u) u = urlFromSource(rec);
      if (u) lines.push(`![图片](${u})`);
    } else if (t === "audio") {
      let u = typeof rec.data === "string" ? rec.data.trim() : "";
      if (!u) u = urlFromSource(rec);
      if (u) lines.push(u);
    } else if (t === "video") {
      let u =
        typeof rec.video_url === "string" ? rec.video_url.trim() : "";
      if (!u) u = urlFromSource(rec);
      if (u) lines.push(u);
    }
  }
  if (lines.length === 0) return "";
  return `\n\n${lines.join("\n")}`;
}

function messageHasNonEmptyText(content: unknown): boolean {
  if (!Array.isArray(content)) return false;
  return content.some((c) => {
    if (!c || typeof c !== "object") return false;
    const o = c as { type?: string; text?: string };
    return (
      o.type === "text" &&
      typeof o.text === "string" &&
      o.text.trim() !== ""
    );
  });
}

function messageHasMedia(content: unknown): boolean {
  if (!Array.isArray(content)) return false;
  return content.some((c) => {
    if (!c || typeof c !== "object") return false;
    const t = (c as { type?: string }).type;
    return (
      t === "file" ||
      t === "image" ||
      t === "audio" ||
      t === "video"
    );
  });
}

function syntheticImageContentItems(urls: string[]) {
  return urls.map((image_url) => ({
    type: "image",
    image_url,
    object: "content",
    delta: false,
    status: STATUS_COMPLETED,
  }));
}

function collectMarkdownImageUrlsFromContent(content: unknown[]): string[] {
  const urls: string[] = [];
  for (const c of content) {
    if (!c || typeof c !== "object") continue;
    const o = c as { type?: string; text?: string };
    if (o.type === "text" && typeof o.text === "string")
      urls.push(...extractMarkdownImageUrls(o.text));
  }
  return urls;
}

function patchMessageIfNeeded(msg: Record<string, unknown>, state: StreamDeltaState) {
  const content = msg.content;
  if (!Array.isArray(content)) return;
  if (!state.hasStreamedDelta) return;

  const augmented = augmentContentArrayWithToolOutputMedia(content);
  if (augmented !== content) msg.content = augmented;

  const merged = msg.content as unknown[];
  const hasMedia = messageHasMedia(merged);
  const hasText = messageHasNonEmptyText(merged);

  if (hasMedia && hasText) return;

  if (!hasMedia && hasText) {
    const urls = collectMarkdownImageUrlsFromContent(merged);
    if (urls.length === 0) return;
    msg.content = [...merged, ...syntheticImageContentItems(urls)];
    return;
  }

  if (hasMedia && !hasText) {
    const appendix = attachmentsAppendixFromContent(merged).trim();
    if (!appendix) return;
    msg.content = [
      ...merged,
      {
        type: "text",
        text: appendix,
        object: "content",
        delta: false,
        status: STATUS_COMPLETED,
      },
    ];
  }
}

function noteDeltaFromLegacyEvent(data: Record<string, unknown>, state: StreamDeltaState) {
  const ev = data.event;
  if (typeof ev !== "string") return;
  if (ev === "chunk" && data.data != null) {
    if (typeof data.data === "string" && data.data)
      state.hasStreamedDelta = true;
    else if (typeof data.data === "object" && data.data !== null) {
      const ch = data.data as Record<string, unknown>;
      if (typeof ch.text === "string" && ch.text) state.hasStreamedDelta = true;
      const ct = ch.type;
      if (ct === "thinking" || ct === "reasoning") state.hasStreamedDelta = true;
    }
  }
}

function transformParsed(data: unknown, state: StreamDeltaState): unknown {
  if (!data || typeof data !== "object") return data;
  const obj = data as Record<string, unknown>;

  if (obj.object === "content" && obj.delta === true) {
    const t = obj.type;
    if (t === "thinking" || t === "reasoning") {
      if (typeof obj.text === "string" && obj.text) state.hasStreamedDelta = true;
      if (typeof obj.thinking === "string" && obj.thinking)
        state.hasStreamedDelta = true;
    }
    if (t === "text" && typeof obj.text === "string" && obj.text)
      state.hasStreamedDelta = true;
    return data;
  }

  noteDeltaFromLegacyEvent(obj, state);

  if (obj.object === "message") {
    patchMessageIfNeeded(obj, state);
    return data;
  }

  if (obj.object === "response" && Array.isArray(obj.output)) {
    for (const m of obj.output) {
      if (m && typeof m === "object")
        patchMessageIfNeeded(m as Record<string, unknown>, state);
    }
    return data;
  }

  return data;
}

export function createCopawStreamResponseParser(state: StreamDeltaState) {
  return {
    reset: () => {
      state.hasStreamedDelta = false;
    },
    parse: (raw: string): unknown => {
      const parsed: unknown = JSON.parse(raw);
      return transformParsed(parsed, state);
    },
  };
}
