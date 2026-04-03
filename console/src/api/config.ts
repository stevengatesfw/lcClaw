declare const VITE_API_BASE_URL: string;
declare const TOKEN: string;

import { getToken } from "./tokenStore";

const AUTH_TOKEN_KEY = "copaw_auth_token";

/**
 * Console is served under LCAgent's `/copaw/` (iframe or same-origin tab).
 * Then API must be `/copaw/api/...` so ingress `location /copaw/api/` reaches Copaw;
 * root `/api/...` is often routed to LCAgent Flask and breaks agents/tools/heartbeat, etc.
 */
function isLcagentCopawMount(): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  return /^\/copaw(?:\/|$)/.test(window.location.pathname);
}

/**
 * Get the full API URL with /api prefix
 * @param path - API path (e.g., "/models", "/skills")
 * @returns Full API URL (e.g., "http://localhost:8088/api/models" or "/api/models" or "/copaw/api/models")
 */
export function getApiUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const apiSuffix = `/api${normalizedPath}`;
  if (isLcagentCopawMount()) {
    return `/copaw${apiSuffix}`;
  }
  const base = typeof VITE_API_BASE_URL !== "undefined" ? VITE_API_BASE_URL || "" : "";
  return `${base}${apiSuffix}`;
}

/**
 * 与 CoPaw 控制台共用：优先 iframe/postMessage 注入的 token，其次本地登录，最后构建期 TOKEN。
 */
export function getApiToken(): string {
  const fromEmbed = getToken();
  if (fromEmbed) {
    return fromEmbed;
  }
  try {
    const stored = localStorage.getItem(AUTH_TOKEN_KEY);
    if (stored) {
      return stored;
    }
  } catch {
    /* ignore */
  }
  return typeof TOKEN !== "undefined" ? TOKEN : "";
}

/**
 * LCAgent Flask console API（嵌入时与父页同源），例如 /console/api/home_config
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
 * Store the auth token in localStorage after login.
 */
export function setAuthToken(token: string): void {
  localStorage.setItem(AUTH_TOKEN_KEY, token);
}

/**
 * Remove the auth token from localStorage (logout / 401).
 */
export function clearAuthToken(): void {
  localStorage.removeItem(AUTH_TOKEN_KEY);
}
