import { useEffect } from "react";
import { SystemTab } from "./components/BottomDock";
import { RegisterWizard } from "./components/RegisterWizard";
import { ConfirmDialog, Toasts, TopBar } from "./components/Shell";
import { TaskComposer } from "./components/TaskComposer";
import { TaskDrawer } from "./components/TaskDrawer";
import { FleetsPage } from "./pages/Fleets";
import { MapEditorPage } from "./pages/MapEditor";
import { OperationsPage } from "./pages/Operations";
import { RobotsPage, TasksPage } from "./pages/RobotsTasks";
import { connectWorld } from "./store/ws";
import { type Page, useWorld } from "./store/world";

export default function App() {
  const page = useWorld((s) => s.page);
  const theme = useWorld((s) => s.theme);
  const wizardOpen = useWorld((s) => s.wizardOpen);
  const composerOpen = useWorld((s) => s.composer.open);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  useEffect(() => {
    connectWorld();
    const onHash = () => {
      const p = window.location.hash.replace("#/", "") as Page;
      if (p) useWorld.setState({ page: p });
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <div className="app">
      <TopBar />
      <main style={{ minHeight: 0, overflow: "hidden" }}>
        {page === "operations" && <OperationsPage />}
        {page === "fleets" && <FleetsPage />}
        {page === "robots" && <RobotsPage />}
        {page === "tasks" && <TasksPage />}
        {page === "map" && <MapEditorPage />}
        {page === "system" && (
          <div className="page">
            <h1 style={{ marginBottom: 14 }}>System health</h1>
            <div className="panel"><SystemTab /></div>
          </div>
        )}
      </main>
      <TaskDrawer />
      {wizardOpen && <RegisterWizard />}
      {composerOpen && <TaskComposer />}
      <ConfirmDialog />
      <Toasts />
    </div>
  );
}
