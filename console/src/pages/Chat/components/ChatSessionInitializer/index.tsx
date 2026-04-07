import React, { useEffect, useMemo, useRef } from "react";
import { useChatAnywhereSessionsState } from "@agentscope-ai/chat";
import { usePinnedChatPathname } from "../../../../hooks/usePinnedChatPathname";

/** sessionApi 在解析 realId 后会把 URL 写成后端 UUID；列表里 id 可能仍是本地时间戳，需按 realId/sessionId 对齐 */
type SessionWithIds = {
  id: string;
  sessionId?: string;
  realId?: string;
};

function sessionMatchesChatId(
  s: { id: string },
  chatId: string,
): boolean {
  if (s.id === chatId) return true;
  const x = s as SessionWithIds;
  if (x.realId && x.realId === chatId) return true;
  if (x.sessionId && x.sessionId === chatId) return true;
  return false;
}

/**
 * URL chatId → context currentSessionId (one direction of bidirectional sync).
 *
 * Only reacts to URL or session list changes. currentSessionId is read via ref
 * to avoid triggering the effect when the context changes from the other direction
 * (context → URL via onSessionSelected), which would cause circular re-loads.
 */
const ChatSessionInitializer: React.FC = () => {
  const pinnedChatPath = usePinnedChatPathname();
  const chatId = useMemo(() => {
    const match = pinnedChatPath.match(/^\/chat\/(.+)$/);
    return match?.[1];
  }, [pinnedChatPath]);

  const { sessions, currentSessionId, setCurrentSessionId } =
    useChatAnywhereSessionsState();

  const currentSessionIdRef = useRef(currentSessionId);
  currentSessionIdRef.current = currentSessionId;

  useEffect(() => {
    if (!chatId || !sessions.length) return;
    const matching = sessions.find((s) => sessionMatchesChatId(s, chatId));
    if (matching && currentSessionIdRef.current !== matching.id) {
      setCurrentSessionId(matching.id);
    }
    // Intentionally exclude currentSessionId from deps: only react to URL / session list changes.
    // currentSessionId is read via ref to avoid circular triggers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId, sessions, setCurrentSessionId]);

  return null;
};

export default ChatSessionInitializer;
