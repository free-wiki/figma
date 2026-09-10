# /// script
# dependencies = ["playwright"]
# ///
"""Fresh browser context: open the embedded app, reload it 3x, navigate inside — the shell must
always win (regression test for the browser-cache collision between shell and proxied page)."""
import asyncio, subprocess, threading, http.server
from playwright.async_api import async_playwright

HOST_HTML = b"<html><body style='margin:0'><iframe id=app src='http://127.0.0.1:8080/' style='width:1200px;height:800px;border:0'></iframe></body></html>"
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("content-type", "text/html"); self.end_headers(); self.wfile.write(HOST_HTML)
    def log_message(self, *a): pass
threading.Thread(target=lambda: http.server.HTTPServer(("127.0.0.1", 8099), H).serve_forever(), daemon=True).start()

async def check(page, label):
    app = page.frame_locator("#app")
    await app.locator("#brandName").wait_for(timeout=15000)
    inner = app.frame_locator("#site")
    await inner.locator("#__ol-pins").wait_for(state="attached", timeout=30000)
    nested = await inner.locator("#brandName").count()
    print(f"{label}: shell OK, inner hasura.io OK, nested shells={nested}")

async def main():
    ep = subprocess.check_output(["promptql-browser-cdp"], text=True).strip()
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(ep)
        ctx = await b.new_context(viewport={"width": 1280, "height": 900})  # fresh cache
        page = await ctx.new_page()
        await page.goto("http://localhost:8099/", wait_until="load"); await check(page, "load 1")
        for i in range(2, 4):
            await page.reload(wait_until="load"); await check(page, f"reload {i}")
        app = page.frame_locator("#app"); inner = app.frame_locator("#site")
        await app.locator("#mBrowse").click()
        link = inner.locator("a[href^='/']:not([href^='/#']):not([href='/'])").first
        href = await link.get_attribute("href")
        print("navigating inner frame to:", href)
        inner_frame = next(f for f in page.frames if f.name == "__ol_frame")
        await inner_frame.evaluate("h => { location.href = h }", href); await page.wait_for_timeout(5000)
        print("after in-frame nav: url bar =", await app.locator("#url").inner_text(), "| nested shells =", await inner.locator("#brandName").count())
        await page.reload(wait_until="load"); await check(page, "reload after nav")
        # pin flow once more
        await app.locator("#mComment").click()
        box = await app.locator("#site").bounding_box()
        await page.mouse.click(box["x"] + 400, box["y"] + 300); await page.wait_for_timeout(600)
        await app.locator("#pText").fill("cache e2e"); await app.locator("#pPost").click(); await page.wait_for_timeout(800)
        print("cards:", await app.locator(".card").count(), "pins:", await inner.locator(".__ol-pin:not(.__ol-draft)").count())
        await page.screenshot(path="/workspace/site-comments/e2e_cache.png")
        n = await page.frames[1].evaluate("async () => { const d = await (await fetch('/__overlay/api/threads')).json(); for (const t of d.threads) await fetch('/__overlay/api/threads/'+t.id,{method:'DELETE'}); return d.threads.length; }")
        print("cleaned:", n)
        await ctx.close()
asyncio.run(main())