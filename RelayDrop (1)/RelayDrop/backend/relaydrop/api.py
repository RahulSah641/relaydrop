"""RelayDrop REST API (Flask).

Endpoints (unchanged from the original service):
  GET  /health, /api/health          POST /api/receive
  GET  /api/session                  POST /api/tickets            GET /api/tickets/<id>?receipt=…
  GET  /api/transfers                POST /api/admin/login        POST /api/admin/logout
  POST /api/transfers                GET  /api/admin/tickets      PATCH /api/admin/tickets/<id>
  PUT  /api/transfers/<id>/files/<file_id>                        GET  /download?token=…[&file_id=…]
  POST /api/transfers/<id>/finalize  DELETE /api/transfers/<id>
New helpers for the Python build (replace client-side JS libraries):
  POST /api/qr  {text, format: svg|png}        POST /api/barcode {code, format: svg|png}
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
import zipfile
from urllib.parse import quote, urlparse, parse_qs

from flask import Blueprint, Response, current_app, g, jsonify, request

from . import barcode128
from .core import (
    DAY, EXPIRY_HOURS, HEX64, MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, HttpError, connect, delete_objects,
    delete_transfer_dir, fail, get_db, is_int, many, now_ms, object_path, one, password_hash, password_input,
    random_hex, safe_equal, safe_names, sha256_hex, text_field,
)
from .qr import QrCode

bp = Blueprint("api", __name__)
log = logging.getLogger("relaydrop")

SECURE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
CHUNK = 256 * 1024


# ------------------------------------------------------------------ plumbing
def json_response(value, status: int = 200) -> Response:
    resp = jsonify(value)
    resp.status_code = status
    resp.headers.update(SECURE_HEADERS)
    return resp


def read_json() -> dict:
    declared = request.content_length
    if declared is not None and declared > 65536:
        fail(413, "Request is too large.")
    raw = request.stream.read(65537)
    if len(raw) > 65536:
        fail(413, "Request is too large.")
    if not raw:
        fail(400, "A JSON request body is required.")
    try:
        import json
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        fail(400, "Invalid JSON request.")
    if not isinstance(data, dict):
        fail(400, "Invalid JSON request.")
    return data


def allowed_origin() -> str | None:
    """The request's Origin if it is listed in CORS_ORIGINS (front-end hosted elsewhere, e.g. GitHub Pages)."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin:
        return None
    if origin in current_app.config.get("CORS_ORIGINS", ()):
        return origin
    # Local development: UI opened from another local port (VS Code Live Server :5500 etc.) while the API runs on this machine.
    loopback = ("localhost", "127.0.0.1")
    if urlparse(origin).hostname in loopback and (request.host.split(":")[0] in loopback):
        return origin
    return None


def cookie_flags() -> dict:
    # Cross-site front-ends need SameSite=None (which browsers only accept together with Secure).
    return {"httponly": True, "secure": request.is_secure or (bool(allowed_origin()) and not is_loopback()), "path": "/"}


def is_loopback() -> bool:
    return request.host.split(":")[0] in ("localhost", "127.0.0.1")


def samesite(default: str) -> str:
    return "None" if allowed_origin() and not is_loopback() and urlparse(allowed_origin()).netloc != request.host else default


@bp.before_request
def identify():
    """Anonymous browser identity: a 256-bit token from the X-Relay-Sender header or the relay_owner cookie."""
    supplied = request.headers.get("x-relay-sender")
    if supplied is not None and not HEX64.match(supplied):
        fail(400, "Your browser session is invalid. Refresh the page and try again.")
    previous = request.cookies.get("relay_owner", "")
    token = supplied if supplied is not None else (previous if HEX64.match(previous) else random_hex())
    g.sender_token = token
    g.set_owner_cookie = previous != token
    g.owner_id = sha256_hex("browser:" + token)
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin")
        if not allowed_origin() and ((origin and urlparse(origin).netloc != request.host) or request.headers.get("sec-fetch-site") == "cross-site"):
            fail(403, "Cross-site requests are not allowed.")


@bp.after_request
def finish(resp: Response) -> Response:
    if getattr(g, "set_owner_cookie", False):
        resp.set_cookie("relay_owner", g.sender_token, max_age=2_592_000, samesite=samesite("Lax"), **cookie_flags())
    origin = allowed_origin()
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Relay-Sender"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        resp.headers.add("Vary", "Origin")
    for k, v in SECURE_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


@bp.errorhandler(HttpError)
def on_http_error(err: HttpError):
    return json_response({"error": err.message, **err.extra}, err.status)


@bp.errorhandler(Exception)
def on_unexpected(err: Exception):
    from werkzeug.exceptions import HTTPException
    if isinstance(err, HTTPException):
        return json_response({"error": err.description or "Request failed."}, err.code or 500)
    log.exception("Unhandled API error")
    return json_response({"error": "Something went wrong. Please try again."}, 500)


def rate(scope: str, limit: int, window_ms: int) -> None:
    db, now = get_db(), now_ms()
    actor = request.remote_addr or g.owner_id
    key = sha256_hex(f"{scope}:{actor}")
    db.execute("BEGIN IMMEDIATE")
    try:
        row = one(db, "SELECT hits, expires_at FROM rate_limits WHERE key=?", key)
        if row is None or row["expires_at"] <= now:
            db.execute("INSERT OR REPLACE INTO rate_limits(key,hits,expires_at) VALUES (?,1,?)", (key, now + window_ms))
            hits = 1
        else:
            hits = row["hits"] + 1
            db.execute("UPDATE rate_limits SET hits=? WHERE key=?", (hits, key))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    if hits > limit:
        fail(429, "Too many attempts. Please wait a little before trying again.")


def is_admin() -> bool:
    raw = request.cookies.get("relay_admin", "")
    if not HEX64.match(raw):
        return False
    return bool(one(get_db(), "SELECT hash FROM admin_sessions WHERE hash=? AND expires_at>?", sha256_hex(raw), now_ms()))


def require_admin() -> None:
    if not is_admin():
        fail(401, "Sign in to the operator inbox first.")


# ------------------------------------------------------------------ transfer helpers
def summary(t: dict, private: bool = False) -> dict:
    status = "expired" if t["status"] == "ready" and t["expires_at"] <= now_ms() else t["status"]
    out = {
        "id": t["id"], "title": t["title"], "note": t["note"], "code": t["code"], "expires_at": t["expires_at"],
        "created_at": t["created_at"], "total_size": t["total_size"], "file_count": t["file_count"],
        "protected": bool(t["password_hash"]), "status": status, "download_count": t["download_count"],
    }
    if private:
        out["share_token"] = t["share_token"]
    return out


def alive(t: dict | None) -> dict:
    if not t or t["status"] != "ready" or t["expires_at"] <= now_ms():
        fail(404, "This transfer is unavailable, expired, or revoked.")
    return t


def resolve_ref(ref: str) -> dict | None:
    r = ref.strip()
    if re.match(r"^https?://", r, re.I):
        try:
            r = (parse_qs(urlparse(r).query).get("receive") or [""])[0]
        except ValueError:
            fail(400, "Enter an eight-digit code or a RelayDrop sharing link.")
    numeric = re.sub(r"[\s-]", "", r)
    db = get_db()
    if re.fullmatch(r"\d{8}", numeric):
        return one(db, "SELECT * FROM transfers WHERE code=?", numeric)
    if HEX64.match(r):
        return one(db, "SELECT * FROM transfers WHERE share_token=?", r)
    return None


def owned(transfer_id: str) -> dict:
    t = one(get_db(), "SELECT * FROM transfers WHERE id=? AND owner=?", transfer_id, g.owner_id)
    if not t:
        fail(404, "Transfer not found in this account or browser.")
    return t


def remove_bytes(db, t: dict) -> None:
    keys = [r["object_key"] for r in many(db, "SELECT object_key FROM files WHERE transfer_id=?", t["id"])]
    delete_objects(keys)
    delete_transfer_dir(t["id"])
    db.execute("UPDATE transfers SET purged_at=? WHERE id=?", (now_ms(), t["id"]))


def revoke(t: dict) -> None:
    db = get_db()
    db.execute("BEGIN")
    db.execute("UPDATE transfers SET status='revoked' WHERE id=?", (t["id"],))
    db.execute("DELETE FROM download_tokens WHERE transfer_id=?", (t["id"],))
    db.execute("COMMIT")
    try:
        remove_bytes(db, t)
    except Exception:  # access stays revoked; cleanup retries removal later
        log.exception("Could not remove revoked transfer bytes")


def cleanup() -> None:
    """Bounded housekeeping, triggered by activity (same strategy as the original)."""
    db, now = get_db(), now_ms()
    stale = many(db, "SELECT * FROM transfers WHERE purged_at IS NULL AND (expires_at<=? OR status='revoked' "
                     "OR (status='staging' AND created_at<?)) LIMIT 8", now, now - 3_600_000)
    for t in stale:
        if t["status"] != "revoked":
            db.execute("UPDATE transfers SET status='expired' WHERE id=?", (t["id"],))
        remove_bytes(db, t)
    db.execute("BEGIN")
    db.execute("DELETE FROM download_tokens WHERE expires_at<?", (now,))
    db.execute("DELETE FROM admin_sessions WHERE expires_at<?", (now,))
    db.execute("DELETE FROM rate_limits WHERE expires_at<?", (now - DAY,))
    db.execute("DELETE FROM tickets WHERE created_at<?", (now - 180 * DAY,))
    db.execute("DELETE FROM transfers WHERE purged_at IS NOT NULL AND MIN(expires_at,purged_at)<?", (now - 30 * DAY,))
    db.execute("COMMIT")


def disposition(name: str) -> str:
    ascii_name = re.sub(r'["\\]', "_", re.sub(r"[^\x20-\x7e]", "_", name))
    encoded = quote(name, safe="~()*!.'-_").replace("(", "%28").replace(")", "%29").replace("*", "%2A").replace("!", "%21").replace("'", "%27")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


# ------------------------------------------------------------------ routes
@bp.route("/health")
@bp.route("/api/health")
def health():
    get_db().execute("SELECT 1").fetchone()
    current_app.config["FILES_DIR"].exists() or fail(503, "File storage is not ready. Please try again shortly.")
    return json_response({"status": "ok", "app": "RelayDrop",
                          "limits": {"files": MAX_FILES, "file_bytes": MAX_FILE_BYTES, "total_bytes": MAX_TOTAL_BYTES}})


@bp.route("/api/session")
def session():
    return json_response({"signedIn": False, "admin": is_admin(), "sender_token": g.sender_token})


@bp.get("/api/transfers")
def list_transfers():
    cleanup()
    rows = many(get_db(), "SELECT * FROM transfers WHERE owner=? AND status!='staging' ORDER BY created_at DESC LIMIT 100", g.owner_id)
    return json_response({"transfers": [summary(t, True) for t in rows]})


@bp.post("/api/transfers")
def create_transfer():
    rate("create-minute", 6, 60_000)
    rate("create-day", 100, DAY)
    cleanup()
    data = read_json()
    files = data.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        fail(400, "Choose between 1 and 20 files.")
    hours = data.get("hours")
    if isinstance(hours, bool) or hours not in EXPIRY_HOURS:
        fail(400, "Choose 1 hour, 24 hours, 3 days, or 7 days.")
    total = 0
    for f in files:
        if (not isinstance(f, dict) or not isinstance(f.get("name"), str) or len(f["name"]) > 1000
                or not is_int(f.get("size")) or f["size"] < 1 or f["size"] > MAX_FILE_BYTES):
            fail(413, "Each file must be non-empty and no larger than 50 MiB.")
        total += int(f["size"])
    if total > MAX_TOTAL_BYTES:
        fail(413, "The total transfer cannot exceed 100 MiB.")
    names = safe_names([f["name"] for f in files])
    password = password_input(data.get("password"))
    salt = random_hex(16) if password else None
    digest = password_hash(password, salt) if password else None
    db = get_db()
    code = ""
    for _ in range(20):
        candidate = f"{secrets.randbelow(10**8):08d}"
        if not one(db, "SELECT id FROM transfers WHERE code=?", candidate):
            code = candidate
            break
    if not code:
        fail(503, "Could not allocate a code. Please try again.")
    tid, now, share = random_hex(16), now_ms(), random_hex()
    entries = [{"id": random_hex(16), "name": names[i], "size": int(f["size"]),
                "mime": (f.get("type") if isinstance(f.get("type"), str) else "application/octet-stream")[:150]}
               for i, f in enumerate(files)]
    title = text_field(data.get("title"), 200, "Title") or names[0]
    note = text_field(data.get("note"), 2000, "Note")
    db.execute("BEGIN")
    try:
        db.execute("INSERT INTO transfers(id,owner,code,share_token,title,note,password_hash,salt,status,created_at,expires_at,"
                   "total_size,file_count,download_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                   (tid, g.owner_id, code, share, title, note, digest, salt, "staging", now, now + int(hours) * 3_600_000, total, len(entries)))
        for e in entries:
            db.execute("INSERT INTO files(id,transfer_id,object_key,name,size,mime,state) VALUES (?,?,?,?,?,?,?)",
                       (e["id"], tid, f"{tid}/{e['id']}", e["name"], e["size"], e["mime"], "pending"))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return json_response({"id": tid, "files": entries}, 201)


@bp.put("/api/transfers/<tid>/files/<fid>")
def upload_file(tid: str, fid: str):
    db = get_db()
    t = owned(tid)
    if t["status"] != "staging" or t["expires_at"] <= now_ms():
        fail(409, "This transfer is no longer accepting uploads.")
    f = one(db, "SELECT * FROM files WHERE id=? AND transfer_id=?", fid, tid)
    if not f:
        fail(404, "File not found.")
    declared = request.content_length
    if declared is not None and declared != f["size"]:
        fail(400, "The uploaded file size does not match.")
    cur = db.execute("UPDATE files SET state='uploading' WHERE id=? AND state='pending'", (fid,))
    if cur.rowcount == 0:
        fail(409, "This file has already been uploaded or is uploading.")
    final = object_path(f["object_key"])
    part = final.with_name(final.name + ".part")
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with open(part, "wb") as out:
            while True:
                chunk = request.stream.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > f["size"]:
                    raise ValueError("Too much data")
                out.write(chunk)
        if written != f["size"]:
            raise ValueError("Size mismatch")
        current = owned(tid)
        if current["status"] != "staging" or current["expires_at"] <= now_ms():
            raise ValueError("Upload cancelled")
        os.replace(part, final)
        db.execute("UPDATE files SET state='uploaded' WHERE id=?", (fid,))
    except Exception:
        part.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        db.execute("UPDATE files SET state='pending' WHERE id=?", (fid,))
        fail(400, "Upload failed or the size did not match. Please retry the transfer.")
    return json_response({"ok": True})


@bp.post("/api/transfers/<tid>/finalize")
def finalize(tid: str):
    db = get_db()
    t = owned(tid)
    if t["status"] != "staging" or t["expires_at"] <= now_ms():
        fail(409, "This transfer can no longer be published.")
    fs = many(db, "SELECT * FROM files WHERE transfer_id=?", tid)
    if len(fs) != t["file_count"] or any(f["state"] != "uploaded" for f in fs):
        fail(409, "Wait until every file has uploaded.")
    size = 0
    for f in fs:
        p = object_path(f["object_key"])
        if not p.is_file() or p.stat().st_size != f["size"]:
            fail(409, "A file is incomplete. Please upload again.")
        size += p.stat().st_size
    if size != t["total_size"] or size > MAX_TOTAL_BYTES:
        fail(413, "The total file size does not match.")
    cur = db.execute("UPDATE transfers SET status='ready' WHERE id=? AND status='staging' AND expires_at>?", (tid, now_ms()))
    if cur.rowcount == 0:
        fail(409, "The transfer was cancelled.")
    return json_response(summary({**t, "status": "ready"}, True))


@bp.delete("/api/transfers/<tid>")
def delete_transfer(tid: str):
    revoke(owned(tid))
    return json_response({"ok": True})


@bp.post("/api/receive")
def receive():
    rate("receive", 15, 60_000)
    data = read_json()
    t = alive(resolve_ref(text_field(data.get("reference"), 2048, "Transfer reference")))
    if t["password_hash"]:
        pw = password_input(data.get("password"))
        if not pw or not safe_equal(password_hash(pw, t["salt"]), t["password_hash"]):
            fail(401, "This transfer requires the correct password.", needs_password=True)
    db = get_db()
    alive(one(db, "SELECT * FROM transfers WHERE id=?", t["id"]))
    token = random_hex()
    db.execute("INSERT INTO download_tokens(hash,transfer_id,expires_at) VALUES (?,?,?)",
               (sha256_hex(token), t["id"], min(t["expires_at"], now_ms() + 900_000)))
    files = many(db, "SELECT id,name,size FROM files WHERE transfer_id=? AND state='uploaded' ORDER BY rowid", t["id"])
    return json_response({"transfer": summary(t), "files": files, "download_token": token})


# ------------------------------------------------------------------ downloads
class _Sink:
    """Write-only, non-seekable buffer so zipfile emits a streamed archive."""

    def __init__(self) -> None:
        self.buf, self.pos = bytearray(), 0

    def write(self, b) -> int:
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = bytes(self.buf)
        self.buf.clear()
        return data


def _file_stream(path, size: int):
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            yield chunk


def _zip_stream(app, transfer_id: str, files: list[dict]):
    """Stream stored (uncompressed) ZIP entries one file at a time, re-checking the transfer between files."""
    conn = connect(app.config["DB_PATH"])
    sink = _Sink()
    try:
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:
            for f in files:
                fresh = one(conn, "SELECT * FROM transfers WHERE id=?", transfer_id)
                if not fresh or fresh["status"] != "ready" or fresh["expires_at"] <= now_ms():
                    raise RuntimeError("Transfer no longer available")
                base = app.config["FILES_DIR"].resolve()
                path = (base / f["object_key"]).resolve()
                info = zipfile.ZipInfo(f["name"], time.localtime()[:6])
                info.compress_type = zipfile.ZIP_STORED
                info.file_size = f["size"]
                info.external_attr = 0o644 << 16
                with open(path, "rb") as src, zf.open(info, "w", force_zip64=f["size"] > 0x7FFFFFFF) as dest:
                    while chunk := src.read(CHUNK):
                        dest.write(chunk)
                        yield sink.take()
                yield sink.take()
        yield sink.take()
    finally:
        conn.close()


@bp.get("/download")
def download():
    db = get_db()
    token = request.args.get("token", "")
    if not HEX64.match(token):
        fail(401, "Open the transfer again to obtain a valid download.")
    grant = one(db, "SELECT transfer_id FROM download_tokens WHERE hash=? AND expires_at>?", sha256_hex(token), now_ms())
    if not grant:
        fail(401, "Your download access has expired. Open the transfer again.")
    t = alive(one(db, "SELECT * FROM transfers WHERE id=?", grant["transfer_id"]))
    file_id = request.args.get("file_id")
    fs = many(db, "SELECT * FROM files WHERE transfer_id=? AND state='uploaded' ORDER BY rowid", t["id"])
    guard = {**SECURE_HEADERS, "Content-Security-Policy": "sandbox; default-src 'none'"}

    def count() -> None:
        cur = db.execute("UPDATE transfers SET download_count=download_count+1 WHERE id=? AND status='ready' AND expires_at>?", (t["id"], now_ms()))
        if cur.rowcount == 0:
            fail(404, "This transfer is unavailable, expired, or revoked.")

    if file_id:
        f = next((x for x in fs if x["id"] == file_id), None)
        if not f:
            fail(404, "File not found in this transfer.")
        path = object_path(f["object_key"])
        if not path.is_file():
            fail(410, "This file is no longer available.")
        count()
        return Response(_file_stream(path, f["size"]), headers={**guard, "Content-Type": "application/octet-stream",
                        "Content-Disposition": disposition(f["name"]), "Content-Length": str(f["size"])})
    for f in fs:
        if not object_path(f["object_key"]).is_file():
            fail(410, "One of these files is no longer available.")
    count()
    app = current_app._get_current_object()
    return Response(_zip_stream(app, t["id"], fs), headers={**guard, "Content-Type": "application/zip",
                    "Content-Disposition": disposition(f"RelayDrop-{t['code']}.zip")})


# ------------------------------------------------------------------ support tickets and operator inbox
@bp.post("/api/tickets")
def create_ticket():
    rate("ticket", 5, 3_600_000)
    data = read_json()
    kind = data.get("kind")
    if kind not in ("contact", "abuse", "copyright"):
        fail(400, "Choose a request type.")
    name = text_field(data.get("name"), 150, "Name")
    email = text_field(data.get("email"), 254, "Email")
    subject = text_field(data.get("subject"), 200, "Subject")
    message = text_field(data.get("message"), 8000, "Message")
    reference = text_field(data.get("reference"), 2048, "Reference")
    signature = text_field(data.get("signature"), 150, "Signature")
    if not name or not subject or len(message) < 20 or not re.fullmatch(r"\S+@\S+\.\S+", email):
        fail(400, "Add your name, a valid email, a subject, and at least 20 characters of detail.")
    if kind != "contact" and not reference:
        fail(400, "Include the transfer link or code you are reporting.")
    if kind == "copyright" and (data.get("declarations") is not True or not signature):
        fail(400, "Sign and confirm the copyright declaration.")
    target = resolve_ref(reference) if reference else None
    receipt, ticket_id, now = random_hex(), "RD-" + random_hex(6).upper(), now_ms()
    get_db().execute(
        "INSERT INTO tickets(id,kind,name,email,subject,message,reference,transfer_id,declarations,signature,receipt_hash,status,response,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ticket_id, kind, name, email, subject, message, reference, target["id"] if target else None,
         1 if data.get("declarations") is True else 0, signature, sha256_hex(receipt), "open", "", now, now))
    return json_response({"id": ticket_id, "receipt": receipt}, 201)


@bp.get("/api/tickets/<tid>")
def ticket_status(tid: str):
    rate("ticket-status", 60, 60_000)
    receipt = request.args.get("receipt", "")
    if not HEX64.match(receipt):
        fail(404, "Request not found.")
    row = one(get_db(), "SELECT id,status,response,created_at FROM tickets WHERE id=? AND receipt_hash=?", tid, sha256_hex(receipt))
    if not row:
        fail(404, "Request not found.")
    return json_response(row)


@bp.post("/api/admin/login")
def admin_login():
    rate("admin-login", 5, 900_000)
    data = read_json()
    key = text_field(data.get("key"), 256, "Admin key")
    expected = current_app.config.get("ADMIN_KEY_HASH")
    if not expected or not safe_equal(sha256_hex(key), expected):
        fail(401, "The operator key is incorrect.")
    token = random_hex()
    get_db().execute("INSERT INTO admin_sessions(hash,expires_at) VALUES (?,?)", (sha256_hex(token), now_ms() + 28_800_000))
    resp = json_response({"ok": True})
    resp.set_cookie("relay_admin", token, max_age=28_800, samesite=samesite("Strict"), **cookie_flags())
    return resp


@bp.post("/api/admin/logout")
def admin_logout():
    get_db().execute("DELETE FROM admin_sessions WHERE hash=?", (sha256_hex(request.cookies.get("relay_admin", "")),))
    resp = json_response({"ok": True})
    resp.set_cookie("relay_admin", "", max_age=0, samesite=samesite("Strict"), **cookie_flags())
    return resp


@bp.get("/api/admin/tickets")
def admin_tickets():
    require_admin()
    cleanup()
    rows = many(get_db(), "SELECT id,kind,name,email,subject,message,reference,transfer_id,declarations,signature,status,response,created_at,updated_at "
                          "FROM tickets ORDER BY created_at DESC LIMIT 200")
    return json_response({"tickets": rows})


@bp.patch("/api/admin/tickets/<tid>")
def admin_update_ticket(tid: str):
    require_admin()
    data = read_json()
    if data.get("status") not in ("open", "reviewing", "resolved", "dismissed"):
        fail(400, "Invalid request status.")
    db = get_db()
    ticket = one(db, "SELECT transfer_id FROM tickets WHERE id=?", tid)
    if not ticket:
        fail(404, "Request not found.")
    if data.get("revoke") is True:
        if not ticket["transfer_id"]:
            fail(400, "No valid RelayDrop transfer is associated with this report.")
        t = one(db, "SELECT * FROM transfers WHERE id=?", ticket["transfer_id"])
        if t:
            revoke(t)
    db.execute("UPDATE tickets SET status=?,response=?,updated_at=? WHERE id=?",
               (data["status"], text_field(data.get("response"), 4000, "Response"), now_ms(), tid))
    return json_response({"ok": True})


# ------------------------------------------------------------------ QR / barcode images (server side, no JS libraries)
@bp.post("/api/qr")
def qr_image():
    data = read_json()
    value = data.get("text")
    if not isinstance(value, str) or not value or len(value) > 1000:
        fail(400, "Provide the text to encode (up to 1000 characters).")
    qr = QrCode(value, "M")
    if data.get("format") == "svg":
        return Response(qr.to_svg(), mimetype="image/svg+xml", headers=SECURE_HEADERS)
    return Response(qr.to_png(280), mimetype="image/png", headers=SECURE_HEADERS)


@bp.post("/api/barcode")
def barcode_image():
    data = read_json()
    value = data.get("code")
    if not isinstance(value, str) or not re.fullmatch(r"[\x20-\x7e]{1,40}", value):
        fail(400, "Provide a short printable code to encode.")
    if data.get("format") == "svg":
        return Response(barcode128.to_svg(value), mimetype="image/svg+xml", headers=SECURE_HEADERS)
    return Response(barcode128.to_png(value), mimetype="image/png", headers=SECURE_HEADERS)


@bp.route("/api/<path:_rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def not_found_api(_rest: str):
    fail(404, "This endpoint does not exist.")
