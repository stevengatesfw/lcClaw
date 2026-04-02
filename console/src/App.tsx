import { createGlobalStyle } from "antd-style";
import { ConfigProvider } from "antd";
import zhCN from "antd/es/locale/zh_CN";
import { BrowserRouter } from "react-router-dom";
import MainLayout from "./layouts/MainLayout";
import { lcagentAntdTheme } from "./theme/lcagentTheme";
import "./styles/layout.css";
import "./styles/form-override.css";

const GlobalStyle = createGlobalStyle`
* {
  margin: 0;
  box-sizing: border-box;
}
`;

function App() {
  // When deployed under /copaw/, router must use basename so pathname matches routes.
  // Avoids "No routes matched location '/copaw/'" when opening /copaw/ directly.
  const basename =
    typeof window !== "undefined" && window.location.pathname.startsWith("/copaw")
      ? "/copaw"
      : "/";

  return (
    <BrowserRouter basename={basename}>
      <GlobalStyle />
      <ConfigProvider locale={zhCN} theme={lcagentAntdTheme}>
        <MainLayout />
      </ConfigProvider>
    </BrowserRouter>
  );
}

export default App;
