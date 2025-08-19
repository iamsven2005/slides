# index.py
from __future__ import annotations
import json
import os
import uuid
from datetime import datetime, timezone

# --- Patch eventlet BEFORE importing Flask/Werkzeug (optional on Windows) ---
async_mode = "threading"
try:
    import eventlet  # type: ignore
    eventlet.monkey_patch()
    async_mode = "eventlet"
except Exception:
    eventlet = None

from flask import Flask, request, redirect, url_for, render_template_string, jsonify, abort, render_template
from flask_socketio import SocketIO, join_room, emit
from sqlalchemy import create_engine, text, bindparam
from sqlalchemy.exc import OperationalError
from sqlalchemy.dialects.postgresql import JSONB
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var missing. Example: postgresql+psycopg2://user:pass@host:5432/dbname")

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=async_mode, json=json)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  content JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
with engine.begin() as conn:
    conn.execute(text(SCHEMA_SQL))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def strip_html(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html or "").strip()


def new_deck(deck_id: str | None = None) -> dict:
    if deck_id is None:
        deck_id = str(uuid.uuid4())
    return {
        "id": deck_id,
        "title": "Untitled deck",
        "slides": [
            {
                "id": str(uuid.uuid4()),
                "title": "Slide 1",
                "background": "#ffffff",
                "objects": [
                    {
                        "id": str(uuid.uuid4()),
                        "type": "text",
                        "x": 120,
                        "y": 120,
                        "width": 520,
                        "height": 70,
                        "rotation": 0,
                        "text": "Double-click (or select) to edit",
                        "fontSize": 32,
                        "fontFamily": "system-ui, Segoe UI, Roboto, sans-serif",
                        "color": "#111111",
                    }
                ],
            }
        ],
    }


def get_deck(deck_id: str) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(text("SELECT id, title, content FROM decks WHERE id=:i"), {"i": deck_id}).fetchone()
        if not row:
            return None
        data = {"id": row.id, "title": row.title, **row.content}
        for s in data.get("slides", []):
            if "objects" not in s:
                s["background"] = s.get("background", "#ffffff")
                objs = []
                for b in s.get("blocks", []):
                    if b.get("type") == "text":
                        objs.append({
                            "id": str(uuid.uuid4()),
                            "type": "text",
                            "x": 100, "y": 100, "width": 500, "height": 80,
                            "rotation": 0,
                            "text": strip_html(b.get("html", "")) or "Text",
                            "fontSize": 28,
                            "fontFamily": "system-ui, Segoe UI, Roboto, sans-serif",
                            "color": "#111111",
                        })
                s["objects"] = objs
                s.pop("blocks", None)
        return data


def upsert_deck(deck_id: str, title: str, content: dict) -> dict:
        def _next_version() -> int:
            with engine.begin() as conn:
                row = conn.execute(
                    text("SELECT (content->>'version')::int AS v FROM decks WHERE id=:i"),
                    {"i": deck_id}
                ).fetchone()
            return (row.v or 0) + 1 if row else 1
        ver = content.get("version")
        if ver is None:
            ver = _next_version()
        payload = {"title": title, "slides": content.get("slides", []), "version": ver}
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO decks (id, title, content)
                    VALUES (:i, :t, :c)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        updated_at = NOW()
                    """
                ).bindparams(bindparam("c", type_=JSONB())),
                {"i": deck_id, "t": title, "c": payload},
            )
    
        return {"id": deck_id, "title": title, **payload}


# ---------------------------- Routes ----------------------------

@app.get("/new")
def new_route():
    d = new_deck()
    upsert_deck(d["id"], d["title"], d)
    return redirect(url_for("home", deck=d["id"]))

@app.get("/")
def home():
    deck_id = request.args.get("deck")
    if not deck_id:
        # Render main page that lists decks
        return render_template("decks.html")
    d = get_deck(deck_id)
    if not d:
        d = new_deck(deck_id)
        upsert_deck(deck_id, d["title"], d)
    return render_template("index.html", deck_id=deck_id)


@app.get("/api/deck/<deck_id>")
def api_get_deck(deck_id):
    d = get_deck(deck_id)
    if not d:
        abort(404)
    return jsonify(d)


@app.post("/api/deck/<deck_id>")
def api_update_deck(deck_id):
    body = request.get_json(force=True)
    title = body.get("title", "Untitled deck")
    slides = body.get("slides", [])
    saved = upsert_deck(deck_id, title, {"slides": slides})
    return jsonify(saved)

@app.get("/api/decks")
def api_list_decks():
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, title, updated_at FROM decks ORDER BY updated_at DESC")).fetchall()
    out = []
    for r in rows:
        # ISO8601 in UTC 'Z' for simple client formatting
        ts = r.updated_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(r.updated_at, "astimezone") else None
        out.append({"id": r.id, "title": r.title, "updated_at": ts})
    return jsonify(out)


@app.get("/healthz")
def healthz():
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}
    except OperationalError as e:
        return {"ok": False, "error": str(e)}, 500


# ---------------------------- WebSocket events ----------------------------
@socketio.on("join_deck")
def on_join_deck(data):
    deck_id = data.get("deck_id")
    if not deck_id:
        return
    join_room(deck_id)
    d = get_deck(deck_id)
    if not d:
        d = new_deck(deck_id)
        upsert_deck(deck_id, d["title"], d)
    emit("deck_state", d)


@socketio.on("content_update")
def on_content_update(data):
    deck_id = data.get("deck_id")
    title = data.get("title", "Untitled deck")
    slides = data.get("slides", [])
    if not deck_id:
        return
    saved = upsert_deck(deck_id, title, {"slides": slides})
    emit("deck_state", saved, room=deck_id, include_self=False)



if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"\nRealtime Slides running: http://localhost:{port}\n")
    socketio.run(app, host="0.0.0.0", port=port)
