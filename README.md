# promptql-collaborative-figma

Figma-style commenting on top of a live website. Drop numbered pins anywhere on the page, reply in threads, resolve them — all attributed to the person who opened the app.

Built as a [PromptQL App Artifact](https://prompt.ql.app) running on a bot VM; the first target site is **hasura.io**.

## How it works

The target site blocks framing (`X-Frame-Options`, `frame-ancestors`), so the app is a **same-origin reverse proxy**:

```
browser ──> app (FastAPI, :8080)
              ├─ /               → comment shell (outer page, sidebar, modes)
              ├─ /...?__frame=1  → proxied site page (headers stripped, URLs rewritten), loaded in the shell's iframe
              ├─ /_next/*, assets→ proxied as-is
              └─ /__overlay/api  → threads / messages / resolve / delete (SQLite)
```

- **Shell vs. frame** is decided from fetch metadata: a same-origin `iframe` load (`Sec-Fetch-Dest: iframe` + `Sec-Fetch-Site: same-origin`) gets the proxied page; everything else gets the shell. `?__frame=1` always forces the proxied page, and the shell contains a loop-breaker in case it is ever served into its own frame.
- **Caching:** all proxied HTML is served `Cache-Control: no-store` with upstream cache headers stripped, and the inner frame loads under a distinct URL. (Without this, the upstream `max-age` caused the browser to serve the plain site instead of the shell on the second open.)
- **Pins** are anchored to the clicked DOM element (CSS selector + relative offset + text snippet) so they survive scroll, resize and reload.
- **Identity** comes from the `X-PromptQL-Visitor-Token` header the PromptQL platform injects (`sub` is the author key, `display_name` the label). Only the author can delete a thread.
- **Storage:** SQLite (`comments.db` next to `app.py`, or `DB_PATH`).

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI service: reverse proxy + overlay API |
| `shell.html` | The overlay UI (Comment/Browse modes, pins, threads, sidebar) |
| `e2e.py` | Headless-browser end-to-end test: pin → post → reply → resolve |
| `e2e_embed.py` | Same, with the app embedded in a cross-origin iframe (as in PromptQL) |
| `e2e_cache.py` | Fresh-context reload/navigation regression test for the shell-vs-frame routing |
| `site-comments.service` | systemd unit used on the VM |

## Run

```bash
uv venv && uv pip install -r requirements.txt
UPSTREAM_ORIGIN=https://hasura.io PORT=8080 .venv/bin/python app.py
# open http://localhost:8080/
```

Keyboard: `C` = Comment mode (click to drop a pin), `V` = Browse mode.

Point it at another site by changing `UPSTREAM_ORIGIN` (e.g. `https://promptql.io`).

## Publish as a PromptQL app artifact (from the bot VM)

```bash
sudo cp site-comments.service /etc/systemd/system/ && sudo systemctl enable --now site-comments
curl -X PUT "$PROMPTQL_PLATFORM_API_URL/v1/artifacts/threads/$PROMPTQL_THREAD_ID/hasura-io-comments-v2" \
  -H "Authorization: Bearer $PROMPTQL_USER_JWT" -H "X-PromptQL-Artifact-Type: app" -H "Content-Type: application/json" \
  -d "{\"version\":2,\"host\":\"vm\",\"sandbox_id\":\"$PROMPTQL_SANDBOX_ID\",\"kind\":\"web\",\"port\":8080,\"protocol\":\"http\",\"readiness\":{\"path\":\"/readyz\"}}"
```

## Tests

```bash
uv run e2e.py && uv run e2e_embed.py && uv run e2e_cache.py
```

(They drive the VM's shared Chrome via `promptql-browser-cdp`; adapt `connect_over_cdp` for a local browser.)