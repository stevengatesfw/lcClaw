import { useEffect, useState } from "react";
import { waitForToken, isInLCAgentIframe } from "../api/tokenStore";

const TOKEN_WAIT_MS = 5000;

/**
 * When embedded in LCAgent iframe, waits up to 5s for token from postMessage.
 * If token arrives, renders children. If timeout, shows "please login" message.
 * When not in iframe (standalone), renders children immediately.
 */
export function AuthGuard({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<"waiting" | "ready" | "timeout">(() =>
    isInLCAgentIframe() ? "waiting" : "ready",
  );

  useEffect(() => {
    if (!isInLCAgentIframe()) {
      setState("ready");
      return;
    }
    waitForToken(TOKEN_WAIT_MS).then((token) => {
      setState(token ? "ready" : "timeout");
    });
  }, []);

  if (state === "ready") {
    return <>{children}</>;
  }
  if (state === "timeout") {
    return (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          height: "100vh",
          padding: 24,
          textAlign: "center",
        }}
      >
        <p style={{ fontSize: 16, marginBottom: 8 }}>
          请先登录 LCAgent 后再使用 CoPaw。
        </p>
        <p style={{ fontSize: 14, color: "#666" }}>
          请从 LCAgent 控制台的 lcClaw 入口打开本页面。
        </p>
      </div>
    );
  }
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        height: "100vh",
      }}
    >
      <p>正在加载…</p>
    </div>
  );
}
