import { useState } from "react";
import { api, setCsrfToken } from "../lib/api";
import { Button, Card, ErrorNote, InfoNote, Input } from "../components/ui";

export default function Login({ needsSetup, onAuthenticated }: { needsSetup: boolean; onAuthenticated: (user: any) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError("");
    if (needsSetup && password !== confirm) {
      setError("The two passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      const path = needsSetup ? "/api/cc/auth/setup" : "/api/cc/auth/login";
      const payload = await api.post<any>(path, { username, password });
      if (payload?.csrf_token) setCsrfToken(payload.csrf_token);
      const me = await api.get<any>("/api/cc/auth/me").catch(() => null);
      if (me?.csrf_token) setCsrfToken(me.csrf_token);
      onAuthenticated(payload?.user ?? me?.user ?? { username });
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto flex min-h-[100dvh] w-full max-w-md flex-col justify-center gap-4 p-5">
      <div className="text-center">
        <div className="text-2xl font-semibold">Hermes Agent</div>
        <div className="text-sm text-[var(--color-muted)]">Control Center</div>
      </div>

      <Card
        title={needsSetup ? "Create your administrator account" : "Sign in"}
        subtitle={needsSetup ? "First run — this account stays on your server." : "Your session cookie is HttpOnly and SameSite=Lax."}
      >
        <ErrorNote message={error} />
        <div className="space-y-3">
          <Input label="Username" value={username} onChange={setUsername} placeholder="admin" autoFocus />
          <Input label="Password" type="password" value={password} onChange={setPassword} placeholder={needsSetup ? "at least 8 characters" : ""} />
          {needsSetup && <Input label="Confirm password" type="password" value={confirm} onChange={setConfirm} />}
          <Button variant="primary" onClick={submit} disabled={busy || !username || password.length < (needsSetup ? 8 : 1)}>
            {busy ? "Working…" : needsSetup ? "Create account and continue" : "Sign in"}
          </Button>
        </div>
        <InfoNote>
          Passwords are hashed with PBKDF2-HMAC-SHA256 and a per-user salt. API keys you add later are encrypted
          server-side and never embedded in the UI bundle.
        </InfoNote>
      </Card>
    </div>
  );
}
