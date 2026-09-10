# /// script
# dependencies = ["playwright"]
# ///
import asyncio, subprocess, sys
from playwright.async_api import async_playwright

async def main():
    ep = subprocess.check_output(["promptql-browser-cdp"], text=True).strip()
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(ep)
        ctx = b.contexts[0] if b.contexts else await b.new_context()
        page = await ctx.new_page()
        page.on("console", lambda m: print("console:", m.type, m.text) if m.type in ("error", "warning") else None)
        await page.set_extra_http_headers({"X-PromptQL-Visitor-Token": "x.eyJzdWIiOiJlMmUtdXNlciIsImRpc3BsYXlfbmFtZSI6IkUyRSBUZXN0ZXIifQ.y"})
        await page.goto("http://127.0.0.1:8080/", wait_until="domcontentloaded")
        fr = page.frame_locator("#site")
        await fr.locator("body").wait_for(timeout=30000)
        await page.wait_for_timeout(2500)
        print("frame url:", page.frames[1].url if len(page.frames) > 1 else None)
        print("pin container present:", await fr.locator("#__ol-pins").count())
        # drop a pin
        box = await page.locator("#site").bounding_box()
        await page.mouse.click(box["x"] + 300, box["y"] + 250)
        await page.wait_for_timeout(500)
        print("draft pin:", await fr.locator(".__ol-pin.__ol-draft").count(), "popover:", await page.locator("#popover.show").count())
        await page.fill("#pText", "E2E comment on the hero")
        await page.click("#pPost")
        await page.wait_for_timeout(800)
        print("pins after post:", await fr.locator(".__ol-pin:not(.__ol-draft)").count(), "cards:", await page.locator(".card").count())
        await page.fill("#pReply", "and a reply")
        await page.click("#pSend")
        await page.wait_for_timeout(800)
        print("messages in popover:", await page.locator("#msgs .msg").count())
        await page.click("#pResolve")
        await page.wait_for_timeout(800)
        print("counts:", await page.locator("#counts").inner_text(), "| visible pins:", await fr.locator(".__ol-pin").count())
        await page.screenshot(path="/workspace/site-comments/e2e.png")
        # cleanup: delete thread
        r = await page.evaluate("""async () => { const d = await (await fetch('/__overlay/api/threads')).json(); for (const t of d.threads) await fetch('/__overlay/api/threads/'+t.id,{method:'DELETE'}); return d.threads.length; }""")
        print("cleaned:", r)
        await page.close()

asyncio.run(main())