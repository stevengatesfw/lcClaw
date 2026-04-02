import { Layout } from "antd";
<<<<<<< HEAD
import { useEffect } from "react";
import { Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
=======
import { Routes, Route, useLocation, Navigate } from "react-router-dom";
>>>>>>> upstream/main
import Sidebar from "../Sidebar";
import Header from "../Header";
import ConsoleCronBubble from "../../components/ConsoleCronBubble";
import styles from "../index.module.less";
import Chat from "../../pages/Chat";
import ChannelsPage from "../../pages/Control/Channels";
import SessionsPage from "../../pages/Control/Sessions";
import CronJobsPage from "../../pages/Control/CronJobs";
import HeartbeatPage from "../../pages/Control/Heartbeat";
import AgentConfigPage from "../../pages/Agent/Config";
import SkillsPage from "../../pages/Agent/Skills";
import SkillPoolPage from "../../pages/Agent/SkillPool";
import ToolsPage from "../../pages/Agent/Tools";
import WorkspacePage from "../../pages/Agent/Workspace";
import MCPPage from "../../pages/Agent/MCP";
import EnvironmentsPage from "../../pages/Settings/Environments";
import SecurityPage from "../../pages/Settings/Security";
import TokenUsagePage from "../../pages/Settings/TokenUsage";
import VoiceTranscriptionPage from "../../pages/Settings/VoiceTranscription";
import AgentsPage from "../../pages/Settings/Agents";

const { Content } = Layout;

const pathToKey: Record<string, string> = {
  "/chat": "chat",
  "/channels": "channels",
  "/sessions": "sessions",
  "/cron-jobs": "cron-jobs",
  "/heartbeat": "heartbeat",
  "/skills": "skills",
  "/skill-pool": "skill-pool",
  "/tools": "tools",
  "/mcp": "mcp",
  "/workspace": "workspace",
  "/agents": "agents",
  "/environments": "environments",
  "/agent-config": "agent-config",
  "/security": "security",
  "/token-usage": "token-usage",
  "/voice-transcription": "voice-transcription",
};

export default function MainLayout() {
  const location = useLocation();
  const currentPath = location.pathname;
  const embedChatOnly =
    typeof window !== "undefined" &&
    new URLSearchParams(location.search).get("embed") === "chat";
  const selectedKey = pathToKey[currentPath] || "chat";

  return (
<<<<<<< HEAD
    <Layout style={{ height: "100vh" }}>
      {!embedChatOnly && <Sidebar selectedKey={selectedKey} />}
      <Layout>
        {!embedChatOnly && <Header selectedKey={selectedKey} />}
=======
    <Layout className={styles.mainLayout}>
      <Header />
      <Layout>
        <Sidebar selectedKey={selectedKey} />
>>>>>>> upstream/main
        <Content className="page-container">
          <ConsoleCronBubble />
          <div className="page-content">
            <Routes>
<<<<<<< HEAD
              <Route path="/chat" element={<Chat />} />
              <Route
                path="/channels"
                element={embedChatOnly ? <Navigate to="/chat" replace /> : <ChannelsPage />}
              />
              <Route
                path="/sessions"
                element={embedChatOnly ? <Navigate to="/chat" replace /> : <SessionsPage />}
              />
              <Route
                path="/cron-jobs"
                element={embedChatOnly ? <Navigate to="/chat" replace /> : <CronJobsPage />}
              />
              <Route
                path="/heartbeat"
                element={embedChatOnly ? <Navigate to="/chat" replace /> : <HeartbeatPage />}
              />
              <Route
                path="/skills"
                element={embedChatOnly ? <Navigate to="/chat" replace /> : <SkillsPage />}
              />
              <Route
                path="/mcp"
                element={embedChatOnly ? <Navigate to="/chat" replace /> : <MCPPage />}
              />
              <Route
                path="/workspace"
                element={embedChatOnly ? <Navigate to="/chat" replace /> : <WorkspacePage />}
              />
              <Route
                path="/environments"
                element={embedChatOnly ? <Navigate to="/chat" replace /> : <EnvironmentsPage />}
              />
              <Route
                path="/agent-config"
                element={embedChatOnly ? <Navigate to="/chat" replace /> : <AgentConfigPage />}
              />
              <Route path="/models" element={<Navigate to="/chat" replace />} />
              <Route path="/" element={<Chat />} />
=======
              <Route path="/" element={<Navigate to="/chat" replace />} />
              <Route path="/chat/*" element={<Chat />} />
              <Route path="/channels" element={<ChannelsPage />} />
              <Route path="/sessions" element={<SessionsPage />} />
              <Route path="/cron-jobs" element={<CronJobsPage />} />
              <Route path="/heartbeat" element={<HeartbeatPage />} />
              <Route path="/skills" element={<SkillsPage />} />
              <Route path="/skill-pool" element={<SkillPoolPage />} />
              <Route path="/tools" element={<ToolsPage />} />
              <Route path="/mcp" element={<MCPPage />} />
              <Route path="/workspace" element={<WorkspacePage />} />
              <Route path="/agents" element={<AgentsPage />} />
              <Route path="/models" element={<ModelsPage />} />
              <Route path="/environments" element={<EnvironmentsPage />} />
              <Route path="/agent-config" element={<AgentConfigPage />} />
              <Route path="/security" element={<SecurityPage />} />
              <Route path="/token-usage" element={<TokenUsagePage />} />
              <Route
                path="/voice-transcription"
                element={<VoiceTranscriptionPage />}
              />
>>>>>>> upstream/main
            </Routes>
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}
