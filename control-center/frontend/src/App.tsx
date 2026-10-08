import { useCallback, useEffect, useState } from "react";
import { api, setCsrfToken } from "./lib/api";
import { useAsync, useHashRoute, useToasts } from "./lib/hooks";
import { Layout, RuntimeChip } from "./components/Layout";
import { Spinner, Toasts } from "./components/ui";

import Dashboard from "./pages/Dashboard";
import Chat from "./pages/Chat";
import Models from "./pages/Models";
import Providers from "./pages/Providers";
import Keys from "./pages/Keys";
import Memory from "./pages/Memory";
import Skills from "./pages/Skills";
import Tools from "./pages/Tools";
import Schedules from "./pages/Schedules";
import Tasks from "./pages/Tasks";
import Sessions from "./pages/Sessions";
import Logs from "./pages/Logs";
import Diagnostics from "./pages/Diagnostics";
import Deploy from "./pages/Deploy";
import Settings from "./pages/Settings";
import Setup from "./pages/Setup";
import Login from "./pages/Login";

type Notify = (message: string, tone?: "ok" | "warn" | "error" | "info") => void;

const SKIP_SETUP_KEY = "cc.skipSetup";

export default function App() {
  const { toasts, push, dismiss } = useToasts();
  const notify = useCallback<Notify>((message, tone) => push(message, tone ?? "info"), [push]);
  const [route, navigate] = useHashRoute();
  const [user, setUser] = useState<{ username: string; role: string } | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [needsSetup, setNeedsSetup] = useState(false);
  const [skipSetup, setSkipSetup] = useState(() => window.localStorage.getItem(SKIP_SETUP_KEY) === "1");

  const auth = useAsync<any>(() => api.get("/api/cc/auth/status"), [], 0);
  const setupState = useAsync<any>(
    () => (authChecked && user ? api.get("/api/cc/settings/setup").catch(() => null) : Promise.resolve(null)),
    [authChecked, Boolean(user)],
    0,
  );
  const runtime = useAsync<any>(() => (user ? api.get("/api/cc/runtime").catch(() => null) : Promise.resolve(null)), [Boolean(user)], 7000);

  useEffect(() => {
    if (!auth.data || authChecked) return;
    setNeedsSetup(Boolean(auth.data.needs_setup));
    if (auth.data.authenticated && auth.data.user) setUser(auth.data.user);
    setAuthChecked(true);
  }, [auth.data, authChecked]);

  useEffect(() => {
    if (!user) return;
    api
      .get<any>("/api/cc/auth/csrf")
      .then((payload) => payload?.csrf_token && setCsrfToken(payload.csrf_token))
      .catch(() => undefined);
  }, [user]);

  const signOut = useCallback(() => {
    setUser(null);
    setCsrfToken("");
    api.get<any>("/api/cc/auth/status").then((payload) => setNeedsSetup(Boolean(payload?.needs_setup))).catch(() => undefined);
    navigate("dashboard");
  }, [navigate]);

  const authenticated = Boolean(user);

  if (!authChecked) {
    return (
      <div className="flex min-h-[100dvh] items-center justify-center">
        <Spinner label="Checking session…" />
      </div>
    );
  }

  if (!authenticated) {
    return (
      <>
        <Login needsSetup={needsSetup} onAuthenticated={(nextUser) => setUser(nextUser)} />
        <Toasts toasts={toasts} onDismiss={dismiss} />
      </>
    );
  }

  const setupCompleted = Boolean(setupState.data?.completed);
  if (!setupCompleted && !skipSetup && route !== "setup") {
    return (
      <>
        <Setup
          notify={notify}
          onDone={() => {
            setSkipSetup(true);
            window.localStorage.setItem(SKIP_SETUP_KEY, "1");
            setupState.reload();
            navigate("dashboard");
          }}
        />
        <Toasts toasts={toasts} onDismiss={dismiss} />
      </>
    );
  }

  const runtimeData = runtime.data?.runtime ?? runtime.data ?? {};
  const common = { notify };

  const page = (() => {
    switch (route) {
      case "chat":
        return <Chat {...common} />;
      case "models":
        return <Models {...common} />;
      case "providers":
        return <Providers {...common} navigate={navigate} />;
      case "keys":
        return <Keys {...common} />;
      case "memory":
        return <Memory {...common} />;
      case "skills":
        return <Skills {...common} />;
      case "tools":
        return <Tools {...common} />;
      case "schedules":
        return <Schedules {...common} />;
      case "tasks":
        return <Tasks {...common} />;
      case "sessions":
        return <Sessions {...common} />;
      case "logs":
        return <Logs {...common} />;
      case "diagnostics":
        return <Diagnostics {...common} />;
      case "deploy":
        return <Deploy />;
      case "setup":
        return <Setup notify={notify} onDone={() => { setupState.reload(); navigate("dashboard"); }} />;
      case "settings":
        return <Settings notify={notify} onLogout={signOut} />;
      case "dashboard":
      default:
        return <Dashboard {...common} navigate={navigate} />;
    }
  })();

  return (
    <>
      <Layout
        route={route}
        navigate={navigate}
        user={user}
        onSignOut={signOut}
        statusChip={<RuntimeChip state={runtimeData.state} healthy={Boolean(runtimeData.healthy)} />}
      >
        {page}
      </Layout>
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </>
  );
}
