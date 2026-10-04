"""临时脚本：扒取 nicevoice 前端 JS，找出 /clone2/tts 的真实请求格式"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.sync_api import sync_playwright

PAGE = "https://nicevoice.org/zh/ai-voice-cloning/"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")
OUT = "/tmp/nv_js"
os.makedirs(OUT, exist_ok=True)

api_calls = []

def on_response(resp):
    url = resp.url
    if "api.nicevoice.org" in url or "/tts" in url or "/clone" in url:
        try:
            body = resp.text()[:2000]
        except Exception:
            body = "<binary/err>"
        api_calls.append({"url": url, "status": resp.status, "method": resp.request.method,
                          "req_headers": {k: v for k, v in resp.request.headers.items()
                                          if k.lower() not in ("cookie",)},
                          "req_body": resp.request.post_data, "resp": body})
        print("[API] %s %s -> %d" % (resp.request.method, url, resp.status))
        print("      req_body: %r" % (resp.request.post_data,))
        print("      resp: %r" % body[:300])

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        storage_state=TOKEN_FILE if os.path.exists(TOKEN_FILE) else None,
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    page = ctx.new_page()
    page.on("response", on_response)

    print("打开页面…")
    page.goto(PAGE, wait_until="networkidle", timeout=90000)

    # 收集所有 script 内容
    scripts = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
    }""")
    print("发现 %d 个外链 JS" % len(scripts))
    for i, src in enumerate(scripts):
        try:
            content = page.evaluate("""async (u) => {
                const r = await fetch(u); return await r.text();
            }""", src)
            fname = os.path.join(OUT, "js_%02d.js" % i)
            with open(fname, "w") as f:
                f.write(content)
            if "clone2" in content or "api.nicevoice" in content or "/tts" in content:
                print(">>> %s 命中 (%d bytes) -> %s" % (src, len(content), fname))
        except Exception as e:
            print("fetch fail %s: %s" % (src, e))

    # 顺便翻页里可能存在的其他入口（音色库等）
    try:
        page.wait_for_timeout(8000)
    except Exception:
        pass
    browser.close()

print("\n=== 页面加载期间捕获的 API 调用 ===")
for c in api_calls:
    print(json.dumps(c, ensure_ascii=False, indent=1)[:1500])
