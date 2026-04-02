import { Layout } from "antd";
import { useEffect } from "react";
import { Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
import Sidebar from "../Sidebar";
import Header from "../Header";
import ConsoleCronBubble from "../../components/ConsoleCronBubble";
import Chat from "../../pages/Chat";
import ChannelsPage from "../../pages/Control/Channels";
import SessionsPage from "../../pages/Control/Sessions";
import CronJobsPage from "../../pages/Control/CronJobs";
import HeartbeatPage from "../../pages/Control/Heartbeat";
import AgentConfigPage from "../../pages/Agent/Config";
import SkillsPage from "../../pages/Agent/Skills";
import WorkspacePage from "../../pages/Agent/Workspace";
import MCPPage from "../../pages/Agent/MCP";
import EnvironmentsPage from "../../pages/Settings/Environments";

const { Content } = Layout;

const pathToKey: Record<string, string> = {
  "/chat": "chat",
  "/channels": "channels",
  "/sessions": "sessions",
  "/cron-jobs": "cron-jobs",
  "/heartbeat": "heartbeat",
  "/skills": "skills",
  "/mcp": "mcp",
  "/workspace": "workspace",
  "/agents": "agents",
  "/environments": "environments",
  "/agent-config": "agent-config",
};

export default function MainLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const currentPath = location.pathname;
  const embedChatOnly =
    typeof window !== "undefined" &&
    new URLSearchParams(location.search).get("embed") === "chat";
  const selectedKey = pathToKey[currentPath] || "chat";

  useEffect(() => {
    if (currentPath === "/") {
      navigate("/chat", { replace: true });
    }
  }, [currentPath, navigate]);

  return (
    <Layout style={{ height: "100vh" }}>
      {!embedChatOnly && <Sidebar selectedKey={selectedKey} />}
      <Layout>
        {!embedChatOnly && <Header selectedKey={selectedKey} />}
        <Content className="page-container">
          <ConsoleCronBubble />
          <div className="page-content">
            <Routes>
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
            </Routes>
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}
