# Migration, backup and restore

Moving this stack to another machine — or to a different deployment shape — is a copy of two directories plus one file.
There is no rebuild, no export/import dance and no vendor in the middle.

---

## What actually holds your state

| Path | Variable | Contains | Portable? |
|---|---|---|---|
| `CC_HOME` (default `~/.hermes/control-center`) | `CC_HOME` | `control-center.db` (settings, users, sessions, chat history, audit, logs), `secret.key` (vault master key), `logs/`, `backups/`, `runtime/` | **Yes** — copy the whole directory |
| `$HERMES_HOME` (default `~/.hermes`) | `CC_HERMES_HOME` | everything the *agent* owns: config, memory, skills, sessions, schedules, its own logs and databases | **Yes** — copy the whole directory |
| The Hermes **checkout** | `CC_HERMES_SOURCE` | the upstream source (code only, no data) | No need — re-clone it |
| Docker volume `hermes-data` | — | both of the above, mounted at `/data` | **Yes** — this is the whole state |

**The one file people forget:** `CC_HOME/secret.key`. Without it, every stored provider key is unreadable. Backups
deliberately exclude it (an archive with ciphertext *and* its key is not encryption), so copy it separately and keep it
somewhere safe.

---

## Docker → Docker (another host)

```bash
# old host
docker compose -f docker/docker-compose.yml stop
docker run --rm -v hermes-control-center_hermes-data:/data -v "$PWD":/out alpine \
  tar czf /out/hermes-data.tgz -C /data .

# new host
git clone <your-repo> && cd <repo>/control-center
cp .env.example .env                     # same CC_ADMIN_PASSWORD, ports, DSN
docker volume create hermes-control-center_hermes-data
docker run --rm -v hermes-control-center_hermes-data:/data -v "$PWD":/in alpine \
  sh -c 'cd /data && tar xzf /in/hermes-data.tgz'
docker compose -f docker/docker-compose.yml up -d --build
```

Confirm the volume name with `docker volume ls` — compose prefixes it with the project name.

## Docker → host install (or the reverse)

```bash
# out of Docker
docker compose -f docker/docker-compose.yml stop
docker run --rm -v hermes-control-center_hermes-data:/data -v "$PWD":/out alpine \
  tar czf /out/hermes-data.tgz -C /data cc hermes
# then, on the host:
mkdir -p ~/.hermes && tar xzf hermes-data.tgz -C ~   # gives ~/cc and ~/hermes
export CC_HOME=$HOME/cc CC_HERMES_HOME=$HOME/hermes
```

Into Docker: copy your local `CC_HOME` and `$HERMES_HOME` into the volume (same commands, reversed), and make sure
`CC_HOME=/data/cc` / `CC_HERMES_HOME=/data/hermes` match the container's expectations.

## Same host, different paths

Stop the app first (a live SQLite file must not be copied mid-write):

```bash
python -m app.cli runtime stop
cp -a "$CC_HOME" /new/path/cc
cp -a "$HERMES_HOME" /new/path/hermes
export CC_HOME=/new/path/cc CC_HERMES_HOME=/new/path/hermes
python -m app.cli doctor
python -m app
```

Permissions: keep `CC_HOME` `0700` and `secret.key` `0600` (`chmod -R go-rwx "$CC_HOME"`).

---

## SQLite → PostgreSQL (optional)

```bash
# 1. stop writing
python -m app.cli runtime stop
export CC_DATABASE_URL='postgresql://user:pass@host:5432/hermes_control_center'
# 2. start once: the schema is created automatically
python -m app.cli list-users        # proves connectivity and creates the schema
# 3. move your settings/users/history with an export/import pair
CC_DATABASE_URL='' python -m app.cli config export --output /tmp/cc.json
python -m app.cli config export --output /tmp/cc-export.json   # same file, either way
```

Then, in the UI on the Postgres instance: **Settings → Import** → choose the JSON. Provider keys are **not** in the
export by design: re-enter them on the Keys page (or copy `secret.key` across so the ciphertext becomes readable again,
then re-import a backup that contains the encrypted rows).

Going back to SQLite is the same in reverse with `CC_DATABASE_URL` emptied.

---

## Backups you can rely on

| Kind | Where | Restores |
|---|---|---|
| **Control Center backup (ZIP)** | `CC_HOME/backups/`, created from Settings → Backups | config, chat, settings, plus a copy of the database |
| **Config export (JSON)** | Settings → Export | settings and history, human-readable, portable across hosts |
| **Hermes backup** | Diagnostics → *Hermes backup* (run by the agent itself) | memory, skills, sessions, schedules |
| **Volume/directory copy** | your shell | everything, including `secret.key` |

A restore that swaps the database must happen **while the app is stopped** — otherwise two writers fight over one SQLite
file. `python -m app.cli reset-db --yes` moves a damaged database aside and creates a fresh one (it warns loudly).

---

## Verify after any migration

```bash
python -m app.cli doctor                       # install + capability
curl -fsS http://127.0.0.1:8080/health         # liveness
python -m app.cli free-routes                  # the free-first chain survived?
```

In the UI:

1. **Diagnostics** — every check green (or an honest informational one).
2. **Dashboard** — runtime running, model selected, FREE MODE on.
3. **Chat** — a turn reaches the agent (the message may still be *"no provider selected"* if you restarted on a host
   without the free route configured — that is a configuration state, not a migration failure).
4. **Models → Fallback chain** — the chain is intact.
5. **Schedules** — jobs exist and their next run is in the future.

If keys show as unreadable after moving `CC_HOME` without `secret.key`: that is expected. Delete and re-add them
(**Keys → Replace**), or restore the master key and the encrypted rows together.
