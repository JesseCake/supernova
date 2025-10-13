"""
LLM System Message Admin — Flask app factory

What this version adds
- `create_app(system_message_path=..., admin_token=..., app_title=...)` so you can configure the file on app creation.
- Optional `create_server(app, host, port)` helper for clean threaded start/stop from another process.
- Same minimal UI + JSON API as before.

Quick start
  $ pip install flask werkzeug
  $ python prompt_admin.py  # still works standalone

From another module
  from prompt_admin import create_app, create_server
  app = create_app(system_message_path="/path/to/prompt.txt", admin_token="secret")
  server = create_server(app, host="0.0.0.0", port=5010)
  server.start()  # run in a background thread
  ...
  server.stop()
"""
from __future__ import annotations

import os
import threading
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, request, Response, jsonify, make_response, current_app
from werkzeug.serving import make_server

# ---------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------

def create_app(
    *,
    system_message_path: str | Path | None = None,
    admin_token: Optional[str] = None,
    app_title: Optional[str] = None,
) -> Flask:
    """Create a configured Flask app.

    Args:
        system_message_path: Path to the text file storing the system prompt.
            Defaults to $SYSTEM_MESSAGE_PATH or ./system_message.txt
        admin_token: Optional bearer token to protect UI & writes.
            Defaults to $ADMIN_TOKEN if not provided.
        app_title: UI title. Defaults to $APP_TITLE or "LLM System Message Admin".
    """
    # Resolve config with env fallbacks
    sys_path = Path(
        system_message_path
        or os.getenv("SYSTEM_MESSAGE_PATH", "./system_message.txt")
    )
    token = admin_token if admin_token is not None else os.getenv("ADMIN_TOKEN")
    title = app_title or os.getenv("APP_TITLE", "LLM System Message Admin")

    # Ensure storage file exists
    sys_path.parent.mkdir(parents=True, exist_ok=True)
    if not sys_path.exists():
        sys_path.write_text(
            "You are a helpful assistant. Keep answers concise.",
            encoding="utf-8",
        )

    app = Flask(__name__)

    # Bind config to app
    app.config.update(
        SYSTEM_MESSAGE_PATH=sys_path,
        ADMIN_TOKEN=token,
        APP_TITLE=title,
    )

    # Per-process lock (atomic write below still protects multi-proc)
    app._prompt_lock = threading.Lock()  # type: ignore[attr-defined]

    # --------------------------- Security ---------------------------
    def require_token(view_fn):
        @wraps(view_fn)
        def wrapper(*args, **kwargs):
            token = current_app.config.get("ADMIN_TOKEN")
            if token:
                auth = request.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth.split(" ", 1)[1] != token:
                    if request.path == "/":
                        return _render_login()
                    return Response("Unauthorized", status=401)
            return view_fn(*args, **kwargs)

        return wrapper

    # --------------------------- Helpers ----------------------------
    def _read_message() -> str:
        path: Path = current_app.config["SYSTEM_MESSAGE_PATH"]
        with app._prompt_lock:  # type: ignore[attr-defined]
            return path.read_text(encoding="utf-8")

    def _write_message(new_text: str) -> None:
        path: Path = current_app.config["SYSTEM_MESSAGE_PATH"]
        tmp_path = path.with_suffix(".tmp")
        data = new_text.replace("", "")
        with app._prompt_lock:  # type: ignore[attr-defined]
            tmp_path.write_text(data, encoding="utf-8")
            tmp_path.replace(path)  # atomic on same filesystem

    def _version_info() -> dict:
        path: Path = current_app.config["SYSTEM_MESSAGE_PATH"]
        stat = path.stat()
        return {
            "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "bytes": stat.st_size,
        }

    def _render_login():
        APP_TITLE = current_app.config["APP_TITLE"]
        html = f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{APP_TITLE} — Login</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }}
    .card {{ max-width: 560px; margin: auto; border: 1px solid #ddd; border-radius: 12px; padding: 1.25rem; }}
    input, button {{ font-size: 16px; padding: .7rem .9rem; }}
    input {{ width: 100%; box-sizing: border-box; }}
    button {{ margin-top: .75rem; }}
  </style>
</head>
<body>
  <div class=\"card\">
    <h1 style=\"margin-top:0\">{APP_TITLE}</h1>
    <p>Enter your admin token to continue.</p>
    <input id=\"token\" type=\"password\" placeholder=\"Bearer token\" />
    <button onclick=\"save()\">Save token</button>
  </div>
  <script>
    function save(){{
      const t = document.getElementById('token').value.trim();
      if(!t){{alert('Token required');return;}}
      localStorage.setItem('prompt_admin_token', t);
      location.reload();
    }}
  </script>
</body>
</html>
"""
        return make_response(html)

    # --------------------------- Routes ----------------------------
    @app.get("/")
    @require_token
    def ui():
        APP_TITLE = current_app.config["APP_TITLE"]
        html = f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{APP_TITLE}</title>
  <style>
    :root {{ --border:#e5e7eb; --bg:#0b0b0b; --fg:#111827; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 1.5rem; color: #111; }}
    .wrap {{ max-width: 900px; margin: auto; }}
    .row {{ display:flex; gap:1rem; align-items:center; justify-content:space-between; flex-wrap:wrap; }}
    textarea {{ width:100%; height: 52vh; padding: 1rem; font: 14px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; border:1px solid var(--border); border-radius: 12px; }}
    .toolbar {{ display:flex; gap:.5rem; align-items:center; margin:.75rem 0 1.25rem; }}
    button {{ border:1px solid #111827; background:#111827; color:white; padding:.65rem 1rem; border-radius:10px; cursor:pointer; }}
    button.secondary {{ background:white; color:#111827; border-color:#cbd5e1; }}
    .muted {{ color:#6b7280; font-size: 12px; }}
    .ok {{ color:#065f46; }}
    .err {{ color:#b91c1c; }}
    .card {{ border:1px solid var(--border); border-radius:12px; padding:1rem; background:white; }}
  </style>
</head>
<body>
  <div class=\"wrap\"> 
    <div class=\"row\">
      <h1 style=\"margin:.25rem 0\">{APP_TITLE}</h1>
      <div class=\"muted\" id=\"version\"></div>
    </div>

    <div class=\"card\">
      <div class=\"toolbar\">
        <button id=\"save\">Update</button>
        <button id=\"reload\" class=\"secondary\">Reload</button>
        <span id=\"status\" class=\"muted\"></span>
      </div>
      <textarea id=\"editor\" spellcheck=\"false\"></textarea>
      <div class=\"muted\" style=\"margin-top:.5rem\">Tip: Use this to edit the <em>system</em> prompt your LLM reads before each conversation.</div>
    </div>

    <div class=\"card\" style=\"margin-top:1rem\">
      <strong>API</strong>
      <pre style=\"white-space: pre-wrap; background:#f8fafc; padding:.75rem; border-radius:8px;\">
GET  /api/system-message
PUT  /api/system-message  {{ \"message\": \"...\" }}
      </pre>
    </div>
  </div>
  <script>
    const token = localStorage.getItem('prompt_admin_token') || '';
    const headers = token ? {{ 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' }} : {{ 'Content-Type': 'application/json' }};

    async function fetchMsg(){{
      const r = await fetch('/api/system-message', {{ headers }});
      if(!r.ok){{ document.getElementById('status').textContent = 'Failed to load'; document.getElementById('status').className='err'; return; }}
      const j = await r.json();
      document.getElementById('editor').value = j.message || '';
      document.getElementById('version').textContent = `Updated: ${j.meta.updated_at} · ${j.meta.bytes} bytes`;
      document.getElementById('status').textContent = '';
    }}

    async function save(){{
      const body = JSON.stringify({{ message: document.getElementById('editor').value }});
      const r = await fetch('/api/system-message', {{ method: 'PUT', headers, body }});
      const el = document.getElementById('status');
      if(r.ok){{
        const j = await r.json();
        el.textContent = 'Saved.'; el.className = 'ok';
        document.getElementById('version').textContent = `Updated: ${j.meta.updated_at} · ${j.meta.bytes} bytes`;
      }} else {{
        el.textContent = 'Save failed'; el.className = 'err';
      }}
    }}

    document.getElementById('save').addEventListener('click', save);
    document.getElementById('reload').addEventListener('click', fetchMsg);
    fetchMsg();
  </script>
</body>
</html>
"""
        return make_response(html)

    @app.get("/api/system-message")
    @require_token
    def get_message():
        msg = _read_message()
        return jsonify({"message": msg, "meta": _version_info()})

    @app.put("/api/system-message")
    @require_token
    def put_message():
        try:
            payload = request.get_json(force=True, silent=False)
        except Exception:
            return Response("Invalid JSON", status=400)
        if not isinstance(payload, dict) or "message" not in payload:
            return Response("JSON body must include 'message'", status=400)
        new_msg = str(payload["message"])  # coerce to str
        _write_message(new_msg)
        return jsonify({"ok": True, "meta": _version_info()})

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app


# ---------------------------------------------------------------------
# Optional threaded server helper (nice for embedding in main.py)
# ---------------------------------------------------------------------
class PromptAdminServer:
    def __init__(self, app: Flask, host: str = "0.0.0.0", port: int = 5000):
        self.server = make_server(host, port, app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=2)


def create_server(app: Flask, host: str = "0.0.0.0", port: int = 5000) -> PromptAdminServer:
    return PromptAdminServer(app, host, port)


# ---------------------------------------------------------------------
# Standalone entrypoint (still supports env var config & PORT)
# ---------------------------------------------------------------------
if __name__ == "__main__":
    app = create_app()  # picks up env vars
    # For production consider: gunicorn -w 2 -b 0.0.0.0:$PORT prompt_admin:create_app()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
