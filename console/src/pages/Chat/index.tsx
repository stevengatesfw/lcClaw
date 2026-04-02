import {
  AgentScopeRuntimeWebUI,
  IAgentScopeRuntimeWebUIOptions,
} from "@agentscope-ai/chat";
import { useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";
import { Modal, Button, Result } from "antd";
import { ExclamationCircleOutlined } from "@ant-design/icons";
import sessionApi from "./sessionApi";
import { useLocalStorageState } from "ahooks";
import defaultConfig, { DefaultConfig } from "./OptionsPanel/defaultConfig";
import Weather from "./Weather";
import LcagentFileArtifact from "./LcagentFileArtifact";
import {
  getApiToken,
  getLcagentConsoleApiUrl,
} from "../../api/config";
import { waitForToken } from "../../api/tokenStore";
import LcagentCapabilityBar from "./LcagentCapabilityBar";
import "./index.module.less";

interface CustomWindow extends Window {
  currentSessionId?: string;
  currentUserId?: string;
  currentChannel?: string;
}

declare const window: CustomWindow;

type OptionsConfig = DefaultConfig;

async function fetchHomeLlmAllowed(): Promise<{ ok: boolean; message?: string }> {
  const token = getApiToken();
  const headers: HeadersInit = {
    "Content-Type": "application/json",
  };
  if (token) {
    (headers as Record<string, string>).Authorization = `Bearer ${token}`;
  }
  try {
    const res = await fetch(getLcagentConsoleApiUrl("/home_config"), {
      method: "GET",
      headers,
      credentials: "same-origin",
    });
    if (!res.ok) {
      return {
        ok: false,
        message: "无法读取首页大模型配置，请确认已登录 LCAgent。",
      };
    }
    const data = await res.json();
    const result = data?.result ?? data;
    if (result?.infer_type === "online" && result?.cloud_model_key) {
      return { ok: true };
    }
    return {
      ok: false,
      message:
        "请先在 LCAgent 系统设置中将首页大模型切换为「云端」并选择模型后再使用 lcClaw（当前版本不支持平台本地推理服务作为 lcagent 模型）。",
    };
  } catch (e) {
    console.error("home_config check failed:", e);
    return {
      ok: false,
      message: "无法连接 LCAgent 控制台接口，请检查网络或登录状态。",
    };
  }
}

export default function ChatPage() {
  const location = useLocation();
  const embedChatOnly =
    typeof window !== "undefined" &&
    new URLSearchParams(location.search).get("embed") === "chat";
  const [showModelPrompt, setShowModelPrompt] = useState(false);
  const [modelPromptMessage, setModelPromptMessage] = useState("");
  const [optionsConfig] = useLocalStorageState<OptionsConfig>(
    "agent-scope-runtime-webui-options",
    {
      defaultValue: defaultConfig,
      listenStorageChange: true,
    },
  );

  const [enableAgent, setEnableAgent] = useLocalStorageState<boolean>(
    "lcagent-chat-enable-agent",
    { defaultValue: false },
  );
  const [enableSkills, setEnableSkills] = useLocalStorageState<boolean>(
    "lcagent-chat-enable-skills",
    { defaultValue: false },
  );
  const [publishedAppId, setPublishedAppId] = useLocalStorageState<string>(
    "lcagent-chat-published-app-id",
    { defaultValue: "" },
  );
  const [publishedAppOptions, setPublishedAppOptions] = useState<
    { value: string; label: string; disabled?: boolean }[]
  >([]);
  const [publishedAppsLoading, setPublishedAppsLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const token = getApiToken() || (await waitForToken(5000));
      if (!token) {
        setPublishedAppOptions([]);
        return;
      }
      setPublishedAppsLoading(true);
      try {
        const res = await fetch(getLcagentConsoleApiUrl("/home/published-apps"), {
          method: "GET",
          headers: {
            Authorization: `Bearer ${token}`,
          },
          credentials: "same-origin",
        });
        if (!res.ok || cancelled) {
          return;
        }
        const json = await res.json();
        const wrap = json?.result ?? json;
        const raw = Array.isArray(wrap?.apps) ? wrap.apps : [];
        const opts = raw.map(
          (row: {
            id: string;
            name: string;
            enable_api?: boolean;
            enable_api_call?: string;
          }) => {
            const apiOk = row.enable_api === true;
            const callOk =
              apiOk && String(row.enable_api_call || "0").trim() === "1";
            let label = row.name || row.id;
            if (!apiOk) {
              label = `${label}（需开启 API）`;
            } else if (!callOk) {
              label = `${label}（需开启 API 调用）`;
            }
            return {
              value: row.id,
              label,
              disabled: !callOk,
            };
          },
        );
        if (!cancelled) {
          setPublishedAppOptions(opts);
        }
      } catch (e) {
        console.error("published apps list failed:", e);
        if (!cancelled) {
          setPublishedAppOptions([]);
        }
      } finally {
        if (!cancelled) {
          setPublishedAppsLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const options = useMemo(() => {
    const handleModelError = (message: string) => {
      setModelPromptMessage(message);
      setShowModelPrompt(true);
      return new Response(
        JSON.stringify({
          error: "Home LLM not configured",
          message,
        }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        },
      );
    };

    const customFetch = async (data: {
      input: any[];
      biz_params?: any;
      signal?: AbortSignal;
    }): Promise<Response> => {
      const allowed = await fetchHomeLlmAllowed();
      if (!allowed.ok) {
        return handleModelError(allowed.message || "首页大模型未就绪");
      }

      const { input, biz_params } = data;

      const lastMessage = input[input.length - 1];
      const session = lastMessage?.session || {};

      const session_id = window.currentSessionId || session?.session_id || "";
      const user_id = window.currentUserId || session?.user_id || "default";
      const channel = window.currentChannel || session?.channel || "console";

      const bizMeta =
        biz_params &&
        typeof biz_params === "object" &&
        biz_params.meta !== undefined &&
        biz_params.meta !== null &&
        typeof biz_params.meta === "object"
          ? { ...(biz_params.meta as Record<string, unknown>) }
          : {};

      const requestBody = {
        ...biz_params,
        input: input.slice(-1),
        session_id,
        user_id,
        channel,
        stream: true,
        meta: {
          ...bizMeta,
          lcagent_enable_agent: Boolean(enableAgent),
          lcagent_enable_skills: Boolean(enableSkills),
          ...(enableAgent && publishedAppId.trim()
            ? { lcagent_published_app_id: publishedAppId.trim() }
            : {}),
        },
      };

      const headers: HeadersInit = {
        "Content-Type": "application/json",
      };

      const token = getApiToken();
      if (token) {
        (headers as Record<string, string>).Authorization = `Bearer ${token}`;
      }

      const url = getLcagentConsoleApiUrl("/copaw/agent/process");
      return fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(requestBody),
        signal: data.signal,
        credentials: "same-origin",
      });
    };

    return {
      ...optionsConfig,
      session: {
        multiple: !embedChatOnly,
        api: sessionApi,
      },
      theme: {
        ...optionsConfig.theme,
      },
      api: {
        ...optionsConfig.api,
        fetch: customFetch,
        cancel(data: { session_id: string }) {
          console.log(data);
        },
      },
      customToolRenderConfig: {
        "weather search mock": Weather,
        invoke_lcagent_published_app: LcagentFileArtifact,
        CallAppTool: LcagentFileArtifact,
      },
    } as unknown as IAgentScopeRuntimeWebUIOptions;
  }, [
    embedChatOnly,
    optionsConfig,
    enableAgent,
    enableSkills,
    publishedAppId,
  ]);

  return (
    <div
      style={{
        height: "100%",
        width: "100%",
        background: "#F5F6F7",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <LcagentCapabilityBar
        enableAgent={Boolean(enableAgent)}
        enableSkills={Boolean(enableSkills)}
        onEnableAgentChange={(v) => setEnableAgent(v)}
        onEnableSkillsChange={(v) => setEnableSkills(v)}
        publishedAppId={publishedAppId || ""}
        publishedAppOptions={publishedAppOptions}
        publishedAppsLoading={publishedAppsLoading}
        onPublishedAppChange={(id) => setPublishedAppId(id)}
      />
      <div style={{ flex: 1, minHeight: 0, minWidth: 0 }}>
        <AgentScopeRuntimeWebUI options={options} />
      </div>

      <Modal
        open={showModelPrompt}
        closable={false}
        footer={null}
        width={480}
        onCancel={() => setShowModelPrompt(false)}
      >
        <Result
          icon={<ExclamationCircleOutlined style={{ color: "#faad14" }} />}
          title="首页大模型未就绪"
          subTitle={modelPromptMessage}
          extra={[
            <Button key="ok" type="primary" onClick={() => setShowModelPrompt(false)}>
              知道了
            </Button>,
          ]}
        />
      </Modal>
    </div>
  );
}
