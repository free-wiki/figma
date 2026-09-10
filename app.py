"""Figma-style comment layer over a proxied website.

- Any path not under /__overlay is reverse-proxied to UPSTREAM_ORIGIN with
  frame-blocking headers (CSP frame-ancestors / X-Frame-Options) stripped and
  absolute upstream URLs rewritten to relative ones, so the site can be loaded
  in a same-origin iframe.
- Top-level navigations (Sec-Fetch-Dest: document) get the overlay shell,
  which iframes the same path. Iframe navigations get the proxied page.
- /__overlay/api/* is the comments API (SQLite). Identity comes from the
  X-PromptQL-Visitor-Token header injected by PromptQL.
"""
import base64
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

UPSTREAM = os.environ.get("UPSTREAM_ORIGIN", "https://hasura.io").rstrip("/")
UPSTREAM_HOST = httpx.URL(UPSTREAM).host
BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DB_PATH", BASE / "comments.db"))
SHELL_PATH = BASE / "shell.html"
PREFIX = "/__overlay"

client: httpx.AsyncClient | None = None
conn: sqlite3.Connection | None = None
READY = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads(
  id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  path TEXT NOT NULL,
  anchor TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0,
  resolved_by TEXT,
  resolved_at REAL,
  created_by TEXT NOT NULL,
  created_by_name TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  body TEXT NOT NULL,
  author_id TEXT NOT NULL,
  author_name TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_thread_path ON threads(path);
"""


@asynccontextmanager
async def lifespan(_app):
    global client, conn, READY
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False)
    READY = True
    try:
        yield
    finally:
        READY = False
        await client.aclose()
        conn.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- identity ---
def visitor(req: Request) -> dict:
    anon = {"id": "anonymous", "name": "Anonymous", "email": None}
    tok = req.headers.get("x-promptql-visitor-token")
    if not tok:
        return anon
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        ns = claims.get("https://promptql.hasura.io") or {}
        uid = claims.get("sub") or ns.get("x-hasura-promptql-user-id")
        if not uid:
            return anon
        email = ns.get("x-hasura-email")
        name = claims.get("display_name") or email or uid[:8]
        return {"id": uid, "name": name, "email": email}
    except Exception:
        return anon


# ---------------------------------------------------------------- storage ----
def load_threads() -> list[dict]:
    rows = conn.execute("SELECT * FROM threads ORDER BY seq").fetchall()
    msgs = conn.execute("SELECT * FROM messages ORDER BY created_at").fetchall()
    by: dict[str, list] = {}
    for m in msgs:
        by.setdefault(m["thread_id"], []).append(dict(m))
    out = []
    for r in rows:
        d = dict(r)
        d["anchor"] = json.loads(d["anchor"])
        d["resolved"] = bool(d["resolved"])
        d["messages"] = by.get(d["id"], [])
        out.append(d)
    return out


def load_thread(tid: str) -> dict:
    r = conn.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()
    if not r:
        raise HTTPException(404, "Thread not found")
    d = dict(r)
    d["anchor"] = json.loads(d["anchor"])
    d["resolved"] = bool(d["resolved"])
    d["messages"] = [dict(m) for m in conn.execute(
        "SELECT * FROM messages WHERE thread_id=? ORDER BY created_at", (tid,))]
    return d


# ---------------------------------------------------------------- api --------
@app.get("/readyz")
async def readyz():
    if not READY or conn is None:
        return Response(status_code=503)
    try:
        conn.execute("SELECT 1").fetchone()
    except Exception:
        return Response(status_code=503)
    return Response(status_code=204)


@app.get(PREFIX + "/api/me")
async def me(req: Request):
    return visitor(req)


@app.get(PREFIX + "/api/threads")
async def list_threads(req: Request):
    return {"me": visitor(req), "threads": load_threads(), "site": UPSTREAM_HOST}


@app.post(PREFIX + "/api/threads")
async def create_thread(req: Request):
    me = visitor(req)
    data = await req.json()
    body = (data.get("body") or "").strip()
    path = data.get("path") or "/"
    anchor = data.get("anchor") or {}
    if not body:
        raise HTTPException(400, "Comment body is required")
    if not isinstance(anchor, dict) or "dx" not in anchor or "dy" not in anchor:
        raise HTTPException(400, "Invalid anchor")
    if len(body) > 5000:
        raise HTTPException(400, "Comment too long")
    now = time.time()
    tid = str(uuid.uuid4())
    seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM threads").fetchone()[0]
    conn.execute(
        "INSERT INTO threads(id,seq,path,anchor,created_by,created_by_name,created_at) VALUES(?,?,?,?,?,?,?)",
        (tid, seq, path, json.dumps(anchor), me["id"], me["name"], now),
    )
    conn.execute(
        "INSERT INTO messages(id,thread_id,body,author_id,author_name,created_at) VALUES(?,?,?,?,?,?)",
        (str(uuid.uuid4()), tid, body, me["id"], me["name"], now),
    )
    return load_thread(tid)


@app.post(PREFIX + "/api/threads/{tid}/messages")
async def add_message(tid: str, req: Request):
    me = visitor(req)
    load_thread(tid)
    data = await req.json()
    body = (data.get("body") or "").strip()
    if not body:
        raise HTTPException(400, "Reply body is required")
    if len(body) > 5000:
        raise HTTPException(400, "Reply too long")
    conn.execute(
        "INSERT INTO messages(id,thread_id,body,author_id,author_name,created_at) VALUES(?,?,?,?,?,?)",
        (str(uuid.uuid4()), tid, body, me["id"], me["name"], time.time()),
    )
    return load_thread(tid)


@app.post(PREFIX + "/api/threads/{tid}/resolve")
async def resolve_thread(tid: str, req: Request):
    me = visitor(req)
    load_thread(tid)
    data = await req.json()
    resolved = bool(data.get("resolved", True))
    if resolved:
        conn.execute("UPDATE threads SET resolved=1, resolved_by=?, resolved_at=? WHERE id=?",
                     (me["id"], time.time(), tid))
    else:
        conn.execute("UPDATE threads SET resolved=0, resolved_by=NULL, resolved_at=NULL WHERE id=?", (tid,))
    return load_thread(tid)


@app.delete(PREFIX + "/api/threads/{tid}")
async def delete_thread(tid: str, req: Request):
    me = visitor(req)
    t = load_thread(tid)
    if t["created_by"] != me["id"]:
        raise HTTPException(403, "Only the author can delete this thread")
    conn.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
    conn.execute("DELETE FROM threads WHERE id=?", (tid,))
    return Response(status_code=204)


# ---------------------------------------------------------------- proxy ------
STRIP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "content-encoding", "content-length",
    "content-security-policy", "content-security-policy-report-only", "x-frame-options",
    "strict-transport-security", "set-cookie", "alt-svc", "report-to", "nel",
    "cross-origin-opener-policy", "cross-origin-embedder-policy", "cross-origin-resource-policy",
}
# Upstream HTML shares URLs with the shell, so it must never be reused from the browser cache.
HTML_STRIP = {"cache-control", "expires", "etag", "last-modified", "age", "pragma", "vary"}
META_REFERRER_RE = re.compile(r"<meta[^>]+name=[\"']?referrer[\"']?[^>]*>", re.I)
FWD = ("accept", "accept-language", "user-agent", "range", "if-none-match", "if-modified-since", "content-type")

_host = re.escape(UPSTREAM_HOST.removeprefix("www."))
ORIGIN_RE = re.compile(r"https?://(?:www\.)?" + _host + r"(?=[/\"'\s?#<)\\]|$)", re.I)
ESC_ORIGIN_RE = re.compile(r"https?:\\/\\/(?:www\.)?" + _host + r"(?=[\\\"'\s?#]|$)", re.I)
META_CSP_RE = re.compile(r"<meta[^>]+http-equiv=[\"']?content-security-policy[\"']?[^>]*>", re.I)


def rewrite_url(u: str) -> str:
    m = ORIGIN_RE.match(u)
    if not m:
        return u
    rest = u[m.end():]
    return rest if rest.startswith("/") else "/" + rest


def rewrite_html(html: str) -> str:
    html = META_CSP_RE.sub("", html)
    html = META_REFERRER_RE.sub("", html)

    def repl(m):
        nxt = html_ref[0][m.end():m.end() + 1]
        return "" if nxt == "/" else "/"

    html_ref = [html]
    html = ORIGIN_RE.sub(repl, html)

    def repl_esc(m):
        nxt = html_ref[0][m.end():m.end() + 2]
        return "" if nxt == "\\/" else "\\/"

    html_ref = [html]
    html = ESC_ORIGIN_RE.sub(repl_esc, html)
    return html


def wants_shell(req: Request, path: str) -> bool:
    """Decide whether this HTML GET is the outer shell or the inner (proxied) frame.

    The app is usually itself embedded in an iframe by the PromptQL console, so
    `Sec-Fetch-Dest: document` is NOT a reliable "top-level" signal. Instead:
    the inner frame is the only requester that is same-origin with us.
    """
    if req.method != "GET":
        return False
    if "__frame" in req.query_params:
        return False
    if "text/html" not in req.headers.get("accept", ""):
        return False
    dest = req.headers.get("sec-fetch-dest")
    site = req.headers.get("sec-fetch-site")
    ref = req.headers.get("referer")
    host = req.headers.get("host", "").split(":")[0].lower()
    if dest is not None:
        if dest == "document":
            shell = True
        elif dest == "iframe":
            shell = site != "same-origin"
        else:
            shell = False
    else:
        rh = None
        if ref:
            try:
                rh = httpx.URL(ref).host.lower()
            except Exception:
                rh = None
        shell = rh != host  # our own page loading the frame -> proxy
    print(f"html-get path=/{path} dest={dest} site={site} ref_host={(httpx.URL(ref).host if ref else None)} host={host} -> {'shell' if shell else 'frame'}", flush=True)
    return shell


def shell_response(req: Request) -> Response:
    html = SHELL_PATH.read_text(encoding="utf-8")
    init = {"path": req.url.path + (("?" + req.url.query) if req.url.query else ""),
            "site": UPSTREAM_HOST, "upstream": UPSTREAM}
    js = "window.__INIT__=" + json.dumps(init).replace("<", "\\u003c") + ";"
    html = html.replace("__INIT_JSON__", js)
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Vary": "Sec-Fetch-Dest, Sec-Fetch-Site"})


async def proxy(req: Request, path: str) -> Response:
    url = f"{UPSTREAM}/{path}"
    qs = [(k, v) for k, v in req.query_params.multi_items() if k != "__frame"]
    if qs:
        url += "?" + str(httpx.QueryParams(qs))
    headers = {k: v for k in FWD if (v := req.headers.get(k))}
    headers["accept-encoding"] = "gzip, deflate"
    body = await req.body() if req.method in ("POST", "PUT", "PATCH") else None
    try:
        r = await client.send(client.build_request(req.method, url, headers=headers, content=body), stream=True)
    except httpx.HTTPError as e:
        return Response(f"Upstream error contacting {UPSTREAM_HOST}: {e}", status_code=502, media_type="text/plain")

    out = {k: v for k, v in r.headers.items() if k.lower() not in STRIP}
    if "location" in r.headers:
        out["location"] = rewrite_url(r.headers["location"])
    ctype = r.headers.get("content-type", "")
    if "text/html" in ctype:
        raw = await r.aread()
        await r.aclose()
        charset = r.charset_encoding or "utf-8"
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        out = {k: v for k, v in out.items() if k.lower() not in HTML_STRIP}
        out["Cache-Control"] = "no-store"
        out["Vary"] = "Sec-Fetch-Dest, Sec-Fetch-Site"
        return Response(rewrite_html(text), status_code=r.status_code, headers=out)
    return StreamingResponse(r.aiter_bytes(), status_code=r.status_code, headers=out,
                             background=BackgroundTask(r.aclose))


@app.api_route("/", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def root(req: Request):
    if wants_shell(req, ""):
        return shell_response(req)
    return await proxy(req, "")


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def catch_all(path: str, req: Request):
    if path.startswith("__overlay") or path.startswith("__promptql"):
        raise HTTPException(404)
    if wants_shell(req, path):
        return shell_response(req)
    return await proxy(req, path)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info",
                proxy_headers=True, forwarded_allow_ips="*")