/**
 * Runtime token store for LCAgent auth.
 * Token is received via postMessage (lcagent:auth) when embedded in LCAgent iframe.
 */
let _token = "";

const _listeners: Array<(token: string) => void> = [];

export function setToken(token: string): void {
  _token = token || "";
  _listeners.forEach((fn) => fn(_token));
}

export function getToken(): string {
  return _token;
}

/**
 * Wait for token to be set, with optional timeout.
 * Resolves immediately if token is already set.
 */
export function waitForToken(timeoutMs: number): Promise<string> {
  if (_token) {
    return Promise.resolve(_token);
  }
  return new Promise<string>((resolve) => {
    const fn = (token: string) => {
      if (token) {
        cleanup();
        resolve(token);
      }
    };
    _listeners.push(fn);
    const t = setTimeout(() => {
      cleanup();
      resolve("");
    }, timeoutMs);
    const cleanup = () => {
      clearTimeout(t);
      const i = _listeners.indexOf(fn);
      if (i >= 0) _listeners.splice(i, 1);
    };
  });
}

export function isInLCAgentIframe(): boolean {
  try {
    return typeof window !== "undefined" && window.self !== window.parent;
  } catch {
    return false;
  }
}


/** LCAgent 控制台与 iframe 内 CoPaw 同域时写入的 key，与 lcagent-k8s front 一致 */
const LCAGENT_CONSOLE_TOKEN_KEY = "console_token";

/**
 * 与 LCAgent 同域嵌入时，父页会在 iframe load 后 postMessage 传 token；
 * 若存在竞态或未及时发送，可直接读同源 localStorage（与父页共用）。
 * 应在应用启动时尽早调用（在 AuthGuard 等待 token 之前）。
 */
export function syncTokenFromLCAgentLocalStorage(): void {
  if (typeof window === "undefined" || !isInLCAgentIframe()) return;
  try {
    const t = window.localStorage?.getItem(LCAGENT_CONSOLE_TOKEN_KEY) || "";
    if (t) setToken(t);
  } catch {
    // localStorage 不可用时忽略
  }
}
