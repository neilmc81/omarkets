import React from "react";
import ReactDOM from "react-dom/client";
import { Root } from "./Root";
import { startThemeSync } from "./theme/omarchy";
import "./style.css";
import "./today/today.css";

startThemeSync();

ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><Root /></React.StrictMode>);
