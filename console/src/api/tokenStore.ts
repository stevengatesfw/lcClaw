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
