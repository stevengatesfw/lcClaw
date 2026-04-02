/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Comma-separated parent origins allowed to send lcagent:auth when cross-origin. */
  readonly VITE_LCAGENT_ALLOWED_ORIGINS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare module "*.less" {
  const classes: { [key: string]: string };
  export default classes;
}

interface PyWebViewAPI {
  open_external_link: (url: string) => void;
}

declare global {
  interface Window {
    pywebview?: {
      api: PyWebViewAPI;
    };
  }
}

export {};
