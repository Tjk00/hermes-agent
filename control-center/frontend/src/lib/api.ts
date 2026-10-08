/**
 * API client. Handles the CSRF header, cookie credentials, structured errors and
 * the SSE stream used by the chat page.
 */

export type ApiError = {
  status: number;
  message: string;
  action?: string;
  raw?: unknown;
};

const API_BASE = (import.meta.env.VITE_CC_API_BASE as string | undefined) ?? "";

let csrfToken = "";

export function setCsrfToken(token: string) {
  csrfToken = token ?? "";
}

export function getCsrfToken() {
  return csrfToken;
}

function buildUrl(path: string, params?: Record<string, unknown>) {
  const url = new URL(`${API_BASE}${path}`, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === "") continue;
      url.searchParams.set(key, String(value));
    }
  }
  return url.pathname + url.search;
}

async function toError(response: Response): Promise<ApiError> {
  let payload: any = null;
  const text = await response.text();
  try {
    payload = JSON.parse(text);
  } catch {
    payload = { message: text.slice(0, 400) };
  }
  const message =
    payload?.message ??
    payload?.detail ??
    payload?.error ??
    (typeof payload === "string" ? payload : `Request failed (HTTP ${response.status})`);
  return {
    status: response.status,
    message: typeof message === "string" ? message : JSON.stringify(message).slice(0, 500),
    action: payload?.action,
    raw: payload,
  };
}

type RequestOptions = {
  method?: string;
  body?: unknown;
  params?: Record<string, unknown>;
  signal?: AbortSignal;
};

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const method = (options.method ?? "GET").toUpperCase();
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) headers["X-CSRF-Token"] = csrfToken;

  const response = await fetch(buildUrl(path, options.params), {
    method,
    headers,
    credentials: "same-origin",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });
  if (!response.ok) throw await toError(response);
  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) return (await response.json()) as T;
  return (await response.text()) as unknown as T;
}

export const api = {
  get: <T>(path: string, params?: Record<string, unknown>) => request<T>(path, { params }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: "PATCH", body }),
  del: <T>(path: string, params?: Record<string, unknown>) => request<T>(path, { method: "DELETE", params }),
};

/** POST a JSON body and consume a Server-Sent Events stream. */
export async function streamSse(
  path: string,
  body: unknown,
  handlers: {
    onEvent: (event: string, data: any) => void;
    onError?: (error: Error) => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "text/event-stream" };
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const response = await fetch(buildUrl(path), {
    method: "POST",
    headers,
    credentials: "same-origin",
    body: JSON.stringify(body),
    signal: handlers.signal,
  });
  if (!response.ok) {
    const error = await toError(response);
    handlers.onError?.(new Error(error.message));
    handlers.onEvent("error", { message: error.message, code: `http_${error.status}` });
    return;
  }
  if (!response.body) {
    handlers.onError?.(new Error("The server returned no stream body."));
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
        let event = "message";
        const dataLines: string[] = [];
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        if (!dataLines.length) continue;
        try {
          handlers.onEvent(event, JSON.parse(dataLines.join("\n")));
        } catch {
          handlers.onEvent(event, { raw: dataLines.join("\n") });
        }
      }
    }
  } catch (error) {
    if ((error as Error).name === "AbortError") return;
    handlers.onError?.(error as Error);
    handlers.onEvent("error", { message: (error as Error).message, code: "stream_error" });
  }
}
