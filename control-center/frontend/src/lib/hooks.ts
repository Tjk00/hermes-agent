import { useCallback, useEffect, useRef, useState } from "react";

export type AsyncState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
};

/** Run an async loader, with optional polling. Keeps the previous data while refetching. */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = [], pollMs = 0): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const result = await loaderRef.current();
        if (!cancelled && alive.current) {
          setData(result);
          setError(null);
        }
      } catch (err) {
        if (!cancelled && alive.current) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled && alive.current) setLoading(false);
      }
    };
    void run();
    if (!pollMs) return () => { cancelled = true; };
    const timer = window.setInterval(run, pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, pollMs, tick]);

  const reload = useCallback(() => {
    setLoading(true);
    setTick((value) => value + 1);
  }, []);

  return { data, error, loading, reload };
}

export type Toast = { id: number; tone: "ok" | "warn" | "error" | "info"; message: string };

export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((message: string, tone: Toast["tone"] = "info") => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current, { id, tone, message }]);
    window.setTimeout(() => setToasts((current) => current.filter((item) => item.id !== id)), 6000);
  }, []);
  const dismiss = useCallback((id: number) => setToasts((current) => current.filter((item) => item.id !== id)), []);
  return { toasts, push, dismiss };
}

/** Hash-based router: works from any static host and any sub-path. */
export function useHashRoute(): [string, (route: string) => void] {
  const [route, setRoute] = useState(() => window.location.hash.replace(/^#\/?/, "") || "dashboard");
  useEffect(() => {
    const onChange = () => setRoute(window.location.hash.replace(/^#\/?/, "") || "dashboard");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  const navigate = useCallback((next: string) => {
    window.location.hash = `#/${next.replace(/^#\/?/, "")}`;
    window.scrollTo({ top: 0 });
  }, []);
  return [route, navigate];
}
