"""RelayDrop — simple, temporary file sharing. Python (Flask) backend: REST API + optional static front-end hosting."""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from .core import close_db, init_db, sha256_hex

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent
DEFAULT_FRONTEND = ROOT_DIR / "frontend"


def create_app(config: dict | None = None) -> Flask:
    try:  # .env is optional (python-dotenv)
        from dotenv import load_dotenv
        load_dotenv(BACKEND_DIR / ".env")
    except ImportError:  # pragma: no cover
        pass

    app = Flask(__name__, static_folder=None)
    data_dir = Path(os.environ.get("RELAYDROP_DATA_DIR") or BACKEND_DIR / "data")
    if not data_dir.is_absolute():
        data_dir = BACKEND_DIR / data_dir
    admin_key = os.environ.get("ADMIN_KEY", "").strip()
    admin_hash = os.environ.get("ADMIN_KEY_HASH", "").strip().lower() or (sha256_hex(admin_key) if admin_key else "")
    frontend = Path(os.environ.get("FRONTEND_DIR") or DEFAULT_FRONTEND)
    cors = tuple(o.strip().rstrip("/") for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip())
    app.config.update(
        DATA_DIR=data_dir,
        FILES_DIR=data_dir / "files",
        DB_PATH=data_dir / "relaydrop.db",
        ADMIN_KEY_HASH=admin_hash,
        FRONTEND_DIR=frontend,
        SERVE_FRONTEND=os.environ.get("SERVE_FRONTEND", "1").lower() not in ("0", "false", "no"),
        CORS_ORIGINS=cors,
    )
    app.json.sort_keys = False
    if config:
        app.config.update(config)

    if os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes"):  # behind Codespaces / Render / nginx
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    init_db(app)
    app.teardown_appcontext(close_db)

    from .api import bp as api_bp
    app.register_blueprint(api_bp)

    @app.get("/", defaults={"page": "index.html"})
    @app.get("/<path:page>")
    def frontend_files(page: str):
        """Serve the static UI (frontend/). Clean URLs such as /about or /guides/send-files-safely work too."""
        root = Path(app.config["FRONTEND_DIR"])
        if not app.config["SERVE_FRONTEND"] or not root.is_dir():
            abort(404)
        for candidate in (page, page + ".html", page.rstrip("/") + "/index.html"):
            if (root / candidate).is_file():
                resp = send_from_directory(root, candidate)
                if candidate.endswith(".html"):
                    resp.headers["Referrer-Policy"] = "no-referrer"
                    resp.headers["X-Content-Type-Options"] = "nosniff"
                    resp.headers["Cache-Control"] = "no-cache"
                return resp
        abort(404)

    @app.errorhandler(404)
    def not_found(_err):
        if request.path.startswith("/api/") or request.path == "/download":
            return jsonify(error="This endpoint does not exist."), 404
        page = Path(app.config["FRONTEND_DIR"]) / "404.html"
        if app.config["SERVE_FRONTEND"] and page.is_file():
            resp = send_from_directory(page.parent, "404.html")
            resp.status_code = 404
            return resp
        return jsonify(error="Not found."), 404

    return app
