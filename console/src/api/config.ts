declare const BASE_URL: string;

import { getToken } from "./tokenStore";

/**
 * Get the full API URL with /api prefix
 * @param path - API path (e.g., "/models", "/skills")
 * @returns Full API URL (e.g., "http://localhost:8088/api/models" or "/api/models")
 */
export function getApiUrl(path: string): string {
  const base = typeof BASE_URL !== "undefined" ? BASE_URL || "" : "";
  const apiPrefix = "/api";
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${apiPrefix}${normalizedPath}`;
}

/**
 * Get the API token (runtime, from LCAgent postMessage when embedded)
 * @returns API token string or empty string
 */
export function getApiToken(): string {
  return getToken();
}

/**
 * LCAgent Flask console API (same origin as parent when embedded), e.g. /console/api/home_config
 */
export function getLcagentConsoleApiUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const prefix = "/console/api";
  if (typeof window === "undefined") {
    return `${prefix}${normalizedPath}`;
  }
  return `${window.location.origin}${prefix}${normalizedPath}`;
}

/**
 * 静态资源与主站对齐（如 `/static/upload/`）：用于将相对路径补成绝对 URL。
 * 若 Vite 配置了绝对 `BASE_URL`，优先使用该源的 origin（与 Console API 同部署时常一致）。
 */
export function getLcagentPublicOrigin(): string {
  const base = typeof BASE_URL !== "undefined" ? BASE_URL || "" : "";
  if (base && /^https?:\/\//i.test(base)) {
    try {
      return new URL(base).origin;
    } catch {
      /* ignore */
    }
  }
  if (typeof window !== "undefined") {
    return window.location.origin;
  }
  return "";
}
