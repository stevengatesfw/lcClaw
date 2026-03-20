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
