import { Card, Image, Typography } from "antd";
import { useMemo } from "react";
import { getLcagentPublicOrigin } from "../../api/config";
import styles from "./LcagentFileArtifact.module.less";

type ToolRendererProps = {
  data?: {
    content?: Array<{
      data?: {
        output?: string;
      };
    }>;
  };
};

type LinkItem = {
  url: string;
  kind: "image" | "video" | "audio" | "pdf" | "other";
};

const URL_RE = /https?:\/\/[^\s)]+/g;

/** 主站常见产物路径：`/static/upload/...` 等（与绝对 URL 分开匹配）。 */
const STATIC_PATH_RE = /\/static\/[^\s\)\]'"‘’「」>]+/g;

function resolveToAbsoluteUrl(raw: string, origin: string): string {
  const t = raw.trim();
  if (/^https?:\/\//i.test(t)) {
    return t;
  }
  if (t.startsWith("//")) {
    if (!origin) {
      return t;
    }
    const proto = origin.startsWith("https") ? "https:" : "http:";
    return `${proto}${t}`;
  }
  if (t.startsWith("/") && origin) {
    return `${origin.replace(/\/$/, "")}${t}`;
  }
  return t;
}

function getKind(url: string): LinkItem["kind"] {
  const clean = url.split("?")[0].toLowerCase();
  if (/\.(png|jpe?g|gif|webp|bmp|svg)$/.test(clean)) return "image";
  if (/\.(mp4|webm|mov|m4v)$/.test(clean)) return "video";
  if (/\.(mp3|wav|ogg|m4a|aac)$/.test(clean)) return "audio";
  if (/\.pdf$/.test(clean)) return "pdf";
  return "other";
}

function extractOutput(data: ToolRendererProps["data"]): string {
  const content = data?.content;
  if (!Array.isArray(content) || content.length === 0) {
    return "";
  }
  const output = content[1]?.data?.output ?? content[0]?.data?.output ?? "";
  if (typeof output !== "string") {
    return "";
  }
  return output;
}

export default function LcagentFileArtifact(props: ToolRendererProps) {
  const { text, links } = useMemo(() => {
    const output = extractOutput(props.data);
    const origin = getLcagentPublicOrigin();
    const httpMatches = output.match(URL_RE) || [];
    const pathMatches = output.match(STATIC_PATH_RE) || [];
    const absolute = [
      ...httpMatches.map((u) => resolveToAbsoluteUrl(u, origin)),
      ...pathMatches.map((p) => resolveToAbsoluteUrl(p, origin)),
    ];
    const urls = absolute.map((url) => ({
      url,
      kind: getKind(url),
    }));
    const uniq = Array.from(new Map(urls.map((x) => [x.url, x])).values());
    return { text: output, links: uniq };
  }, [props.data]);

  if (!links.length && !text.trim()) {
    return null;
  }

  return (
    <Card size="small" className={styles.card}>
      <div className={styles.title}>应用产物预览</div>
      {text.trim() ? (
        <Typography.Paragraph copyable={{ text }} ellipsis={{ rows: 4, expandable: true }}>
          {text}
        </Typography.Paragraph>
      ) : null}

      {links.map((item) => (
        <div key={item.url} className={styles.previewBlock}>
          {item.kind === "image" ? (
            <Image src={item.url} alt="artifact" className={styles.media} />
          ) : null}
          {item.kind === "video" ? (
            <video controls src={item.url} className={styles.media} />
          ) : null}
          {item.kind === "audio" ? (
            <audio controls src={item.url} style={{ width: "100%" }} />
          ) : null}
          {item.kind === "pdf" ? (
            <iframe src={item.url} title="pdf-preview" className={styles.iframe} />
          ) : null}
          {item.kind === "other" ? (
            <Typography.Link href={item.url} target="_blank" rel="noreferrer">
              打开文件: {item.url}
            </Typography.Link>
          ) : (
            <div className={styles.urlList}>
              <Typography.Link href={item.url} target="_blank" rel="noreferrer">
                在新窗口打开: {item.url}
              </Typography.Link>
            </div>
          )}
        </div>
      ))}
    </Card>
  );
}
