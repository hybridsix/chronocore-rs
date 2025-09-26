# backend/server.py
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from starlette.staticfiles import StaticFiles

APP = FastAPI(title="PRS Backend", version="0.1.1")

APP.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create/serve a static folder for simple UIs (HTML/JS/CSS)
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
APP.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "laps.sqlite"

@APP.get("/debug/static-path")
async def debug_static_path():
    return {"static_dir": str(STATIC_DIR), "exists": STATIC_DIR.exists()}


async def table_has_column(db: aiosqlite.Connection, table: str, col: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        async for row in cur:  # row = (cid, name, type, notnull, dflt_value, pk)
            if row[1] == col:
                return True
    return False


def row_to_dict(row, keys: List[str]) -> Dict[str, Any]:
    return {k: row[i] for i, k in enumerate(keys) if i < len(row)}


@APP.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "db_exists": DB_PATH.exists()}


@APP.get("/laps")
async def laps(
    limit: int = Query(10, ge=1, le=1000),
    race_id: Optional[int] = Query(None, description="Optional race filter (only if column exists)"),
) -> Dict[str, Any]:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    async with aiosqlite.connect(DB_PATH) as db:
        has_race_id = await table_has_column(db, "passes", "race_id")

        select_cols = [
            "id", "host_ts_utc", "port", "decoder_id", "tag_id", "decoder_secs", "raw_line"
        ]
        sql_cols = ", ".join(select_cols)
        keys = select_cols.copy()

        if has_race_id:
            sql_cols += ", race_id"
            keys.append("race_id")

        query = f"SELECT {sql_cols} FROM passes"
        params: List[Any] = []

        if has_race_id and race_id is not None:
            query += " WHERE race_id = ?"
            params.append(race_id)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        items: List[Dict[str, Any]] = []
        async with db.execute(query, params) as cur:
            async for row in cur:
                items.append(row_to_dict(row, keys))

    return {"count": len(items), "items": items}
