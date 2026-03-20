import { createRoot } from "react-dom/client";
import App from "./App.tsx";
import "./i18n";
import { setToken } from "./api/tokenStore";
import { AuthGuard } from "./components/AuthGuard";

/** LCAgent auth message type - parent sends token when embedding lcClaw */
const LCAGENT_AUTH_MESSAGE_TYPE = "lcagent:auth";

if (typeof window !== "undefined") {
  // Fallback: token injected by LCAgent proxy (window.__COPAW_TOKEN)
  const injected = (window as unknown as { __COPAW_TOKEN?: string }).__COPAW_TOKEN;
  if (injected) setToken(injected);

  window.addEventListener("message", (event) => {
    if (event.data?.type === LCAGENT_AUTH_MESSAGE_TYPE && event.data?.token) {
      setToken(event.data.token);
    }
  });
}

if (typeof window !== "undefined") {
  const originalError = console.error;
  const originalWarn = console.warn;

  console.error = function (...args: any[]) {
    const msg = args[0]?.toString() || "";
    if (msg.includes(":first-child") || msg.includes("pseudo class")) {
      return;
    }
    originalError.apply(console, args);
  };

  console.warn = function (...args: any[]) {
    const msg = args[0]?.toString() || "";
    if (
      msg.includes(":first-child") ||
      msg.includes("pseudo class") ||
      msg.includes("potentially unsafe")
    ) {
      return;
    }
    originalWarn.apply(console, args);
  };
}

createRoot(document.getElementById("root")!).render(
  <AuthGuard>
    <App />
  </AuthGuard>
);
