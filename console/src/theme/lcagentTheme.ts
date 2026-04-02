import type { ThemeConfig } from "antd";

/** Aligned with front/theme-skins/theme-config.ts (LCAgent homepage). */
export const lcagentAntdTheme: ThemeConfig = {
  token: {
    colorPrimary: "#0E5DD8",
    colorLink: "#0E5DD8",
    borderRadius: 4,
  },
  components: {
    Dropdown: {
      controlItemBgHover: "#F2F6FF",
    },
  },
};
