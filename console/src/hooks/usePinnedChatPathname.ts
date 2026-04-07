import { useEffect, useMemo } from "react";
import { useLocation } from "react-router-dom";

const STORAGE_KEY = "copaw_pinned_chat_path";

/**
 * lcClaw 在 MainLayout 中持久挂载时，离开 /chat 后全局 location 会变成 /channels 等，
 * 若仍用 location.pathname 解析 chatId，会话与 URL 会错位。在位于 /chat 时写入
 * sessionStorage，离开后用上次路径继续驱动 sessionApi / preferredChatId。
 */
export function usePinnedChatPathname(): string {
  const location = useLocation();

  useEffect(() => {
    const p = location.pathname;
    if (p === "/chat" || p.startsWith("/chat/")) {
      try {
        sessionStorage.setItem(STORAGE_KEY, p);
      } catch {
        /* ignore */
      }
    }
  }, [location.pathname]);

  return useMemo(() => {
    const p = location.pathname;
    if (p === "/chat" || p.startsWith("/chat/")) {
      return p;
    }
    try {
      const s = sessionStorage.getItem(STORAGE_KEY);
      if (s && (s === "/chat" || s.startsWith("/chat/"))) {
        return s;
      }
    } catch {
      /* ignore */
    }
    return "/chat";
  }, [location.pathname]);
}
