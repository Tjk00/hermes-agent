"""Logs: our own store, the runtime process output, and Hermes' own log file."""

from __future__ import annotations

import csv
import io
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..container import Container
from ..db import utcnow
from ..deps import ContainerDep, Principal, mutation_principal, require_admin
from ..hermes.client import HermesAPIError, HermesUnavailable
from ..logbus import CATEGORIES
from ..security import get_redactor

router = APIRouter(prefix="/logs", tags=["logs"])

SOURCE_CHOICES = ("control-center", "runtime", "hermes", "all")


@router.get("/categories")
async def categories(principal: Annotated[Principal, Depends(require_admin)]) -> dict:
    return {
        "categories": list(CATEGORIES),
        "sources": [
            {"id": "control-center", "label": "Control Center", "description": "App events, security, providers, models."},
            {"id": "runtime", "label": "Runtime process", "description": "stdout/stderr of the Hermes process this app supervises."},
            {"id": "hermes", "label": "Hermes agent", "description": "Hermes' own log file, read through its API."},
            {"id": "all", "label": "Everything", "description": "Merged view (control center entries keep their filters)."},
        ],
    }


@router.get("")
async def read_logs(
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
    source: Literal["control-center", "runtime", "hermes", "all"] = "control-center",
    level: str = Query("", max_length=16),
    category: str = Query("", max_length=24),
    q: str = Query("", max_length=200),
    limit: int = Query(300, ge=1, le=2000),
) -> dict:
    redactor = get_redactor()
    payload: dict = {"sources": {}, "categories": list(CATEGORIES)}

    if source in {"control-center", "all"}:
        clauses, params = [], []
        if level:
            clauses.append("level = ?")
            params.append(level.upper())
        if category:
            clauses.append("category = ?")
            params.append(category.upper())
        if q:
            clauses.append("message LIKE ?")
            params.append(f"%{q}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = container.db.query(
            f"SELECT id, ts, level, category, message, meta FROM log_events {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )
        payload["sources"]["control-center"] = {
            "entries": [
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "level": row["level"],
                    "category": row["category"],
                    "message": redactor.redact(row["message"]),
                    "meta": json.loads(row["meta"]) if row.get("meta") else None,
                }
                for row in rows
            ]
        }

    if source in {"runtime", "all"}:
        lines = [redactor.redact(line) for line in container.supervisor.log_lines(limit)]
        if q:
            lines = [line for line in lines if q.lower() in line.lower()]
        payload["sources"]["runtime"] = {
            "entries": [{"ts": "", "level": _guess_level(line), "category": "RUNTIME", "message": line} for line in lines],
            "file": str(container.supervisor.log_file),
        }

    if source in {"hermes", "all"}:
        entries: list[dict] = []
        error = ""
        try:
            raw = await container.supervisor.client().get("/api/logs", params={"lines": limit}, timeout=45)
            lines = raw.get("lines") if isinstance(raw, dict) else raw
            if isinstance(lines, list):
                entries = [
                    {"ts": "", "level": _guess_level(str(line)), "category": "HERMES", "message": redactor.redact(str(line))}
                    for line in lines
                ]
        except (HermesAPIError, HermesUnavailable) as exc:
            error = exc.human_detail() if isinstance(exc, HermesAPIError) else str(exc)
        payload["sources"]["hermes"] = {"entries": entries, "error": error}

    if source == "all":
        merged: list[dict] = []
        for key, block in payload["sources"].items():
            for entry in block.get("entries", []):
                merged.append({**entry, "source": key})
        payload["merged"] = merged[:limit]

    return payload


@router.get("/summary")
async def log_summary(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    rows = container.db.query("SELECT level, category, COUNT(*) AS n FROM log_events GROUP BY level, category")
    by_level: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for row in rows:
        by_level[row["level"]] = by_level.get(row["level"], 0) + int(row["n"])
        by_category[row["category"]] = by_category.get(row["category"], 0) + int(row["n"])
    return {
        "by_level": by_level,
        "by_category": by_category,
        "total": sum(by_level.values()),
        "runtime_log_bytes": container.supervisor.log_file.stat().st_size if container.supervisor.log_file.exists() else 0,
        "retention_days": container.db.get_setting("log_retention_days", container.settings.log_retention_days),
    }


@router.delete("")
async def clear_logs(
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
    source: Literal["control-center", "runtime", "hermes"] = "control-center",
) -> dict:
    if source == "control-center":
        removed = container.db.execute("DELETE FROM log_events")
    elif source == "runtime":
        path = container.supervisor.log_file
        if not path.exists():
            raise HTTPException(status_code=404, detail="No runtime log file yet.")
        path.write_text("", encoding="utf-8")
        removed = 0
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "Hermes' own log files belong to Hermes. Rotate or remove them yourself in $HERMES_HOME/logs — "
                "this app will not delete the agent's logs behind your back."
            ),
        )
    container.audit("logs_cleared", actor=principal.username, target=source, level="warning")
    return {"ok": True, "removed": removed, "source": source}


@router.get("/export")
async def export_logs(
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
    format: Literal["json", "csv", "txt"] = "json",
    limit: int = Query(2000, ge=1, le=20_000),
) -> StreamingResponse:
    redactor = get_redactor()
    rows = container.db.query("SELECT ts, level, category, message FROM log_events ORDER BY id DESC LIMIT ?", (limit,))
    for row in rows:
        row["message"] = redactor.redact(row["message"])
    stamp = utcnow().replace(":", "").replace("-", "")
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=["ts", "level", "category", "message"])
        writer.writeheader()
        writer.writerows(rows)
        body, media = buffer.getvalue(), "text/csv"
    elif format == "txt":
        body = "\n".join(f"{row['ts']} {row['level']} {row['category']}: {row['message']}" for row in rows)
        media = "text/plain"
    else:
        body = json.dumps({"generated_at": utcnow(), "entries": rows}, indent=2)
        media = "application/json"
    return StreamingResponse(
        iter([body]),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="control-center-logs-{stamp}.{format}"'},
    )


def _guess_level(line: str) -> str:
    upper = line.upper()
    for level in ("ERROR", "CRITICAL", "WARNING", "WARN"):
        if level in upper:
            return "ERROR" if level in {"ERROR", "CRITICAL"} else "WARNING"
    return "INFO"
