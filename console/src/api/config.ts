declare const VITE_API_BASE_URL: string;
declare const TOKEN: string;

import { getToken } from "./tokenStore";

const AUTH_TOKEN_KEY = "copaw_auth_token";

/**
 * Get the full API URL with /api prefix
 * @param path - API path (e.g., "/models", "/skills")
 * @returns Full API URL (e.g., "http://localhost:8088/api/models" or "/api/models")
 */
export function getApiUrl(path: string): string {
  const base = typeof VITE_API_BASE_URL !== "undefined" ? VITE_API_BASE_URL || "" : "";
  const apiPrefix = "/api";
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${apiPrefix}${normalizedPath}`;
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
