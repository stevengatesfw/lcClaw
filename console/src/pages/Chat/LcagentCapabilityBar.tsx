import { Switch, Tooltip, Space, Select } from "antd";
import { AppWindow, Bot, Wrench } from "lucide-react";
import styles from "./LcagentCapabilityBar.module.less";

export type LcagentCapabilityBarProps = {
  enableAgent: boolean;
  enableSkills: boolean;
  onEnableAgentChange: (v: boolean) => void;
  onEnableSkillsChange: (v: boolean) => void;
  publishedAppId: string;
  publishedAppOptions: { value: string; label: string; disabled?: boolean }[];
  publishedAppsLoading: boolean;
  onPublishedAppChange: (appId: string) => void;
};

/**
 * P0：首页 / lcClaw 聊天区顶部开关，通过 meta 透传给 LCAgent → CoPaw。
 * CoPaw 侧是否消费由后续 P1 实现；此处仅保证请求体可观测。
 */
export default function LcagentCapabilityBar({
  enableAgent,
  enableSkills,
  onEnableAgentChange,
  onEnableSkillsChange,
  publishedAppId,
  publishedAppOptions,
  publishedAppsLoading,
  onPublishedAppChange,
}: LcagentCapabilityBarProps) {
  return (
    <div className={styles.bar}>
      <Space size="large" wrap>
        <Tooltip title="开启后请求 meta 将携带 lcagent_enable_agent，供平台侧调用已发布应用等能力（需 CoPaw/下游实现）">
          <span className={styles.item}>
            <Bot size={16} className={styles.icon} />
            <span className={styles.label}>应用 / Agent</span>
            <Switch
              size="small"
              checked={enableAgent}
              onChange={onEnableAgentChange}
            />
          </span>
        </Tooltip>
        <Tooltip title="选择默认 LCAgent 已发布应用；写入 meta.lcagent_published_app_id，工具 invoke_lcagent_published_app 可将 app_id 留空">
          <span className={styles.item}>
            <AppWindow size={16} className={styles.icon} />
            <span className={styles.label}>默认应用</span>
            <Select
              size="small"
              className={styles.appSelect}
              placeholder={
                publishedAppsLoading ? "加载中…" : "可选：已发布 + API + 可调应用"
              }
              allowClear
              loading={publishedAppsLoading}
              options={publishedAppOptions}
              value={publishedAppId || undefined}
              onChange={(v) =>
                onPublishedAppChange(typeof v === "string" ? v : "")
              }
              disabled={!enableAgent}
            />
          </span>
        </Tooltip>
        <Tooltip title="开启后请求 meta 将携带 lcagent_enable_skills，启用平台技能库相关能力（需 CoPaw/下游实现）">
          <span className={styles.item}>
            <Wrench size={16} className={styles.icon} />
            <span className={styles.label}>平台技能</span>
            <Switch
              size="small"
              checked={enableSkills}
              onChange={onEnableSkillsChange}
            />
          </span>
        </Tooltip>
      </Space>
    </div>
  );
}
