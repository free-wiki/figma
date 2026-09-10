# /// script
# dependencies = ["playwright"]
# ///
"""Simulate the PromptQL console: embed the app in an iframe from a different origin."""
import asyncio, subprocess, threading, http.server
from playwright.async_api import async_playwright

HOST_HTML = b"<html><body style='margin:0'><iframe id=app src='http://127.0.0.1:8080/' style='width:1200px;height:800px;border:0'></iframe></body></html>"

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("content-type", "text/html"); self.end_headers(); self.wfile.write(HOST_HTML)
    def log_message(self, *a): pass

def serve():
    http.server.HTTPServer(("127.0.0.1", 8099), H).serve_forever()

async def main():
    threading.Thread(target=serve, daemon=True).start()
    ep = subprocess.check_output(["promptql-browser-cdp"], text=True).strip()
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(ep)
        ctx = b.contexts[0]
        page = await ctx.new_page()
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.goto("http://localhost:8099/", wait_until="domcontentloaded")
        app = page.frame_locator("#app")
        await app.locator("#brandName").wait_for(timeout=15000)
        print("shell in embedded iframe: OK, brand =", await app.locator("#brandName").inner_text())
        inner = app.frame_locator("#site")
        await inner.locator("#__ol-pins").wait_for(state="attached", timeout=30000)
        print("inner hasura.io frame + pin container: OK")
        # navigate inside the inner frame via a link, make sure it stays a proxied page (no nested shell)
        await app.locator("#mBrowse").click()
        await inner.locator("a[href='/pricing']").first.click()
        await page.wait_for_timeout(4000)
        print("url bar after nav:", await app.locator("#url").inner_text(), "| nested shells:", await inner.locator("#brandName").count())
        await app.locator("#mComment").click()
        box = await app.locator("#site").bounding_box()
        await page.mouse.click(box["x"] + 400, box["y"] + 300)
        await page.wait_for_timeout(600)
        print("draft popover:", await app.locator("#popover.show").count())
        await app.locator("#pText").fill("embedded e2e")
        await app.locator("#pPost").click()
        await page.wait_for_timeout(800)
        print("cards:", await app.locator(".card").count(), "pins:", await inner.locator(".__ol-pin:not(.__ol-draft)").count())
        await page.screenshot(path="/workspace/site-comments/e2e_embed.png")
        n = await page.frames[1].evaluate("async () => { const d = await (await fetch('/__overlay/api/threads')).json(); for (const t of d.threads) await fetch('/__overlay/api/threads/'+t.id,{method:'DELETE'}); return d.threads.length; }")
        print("cleaned:", n)
        await page.close()

asyncio.run(main())