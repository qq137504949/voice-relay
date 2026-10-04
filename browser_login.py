"""浏览器登录 / token 抓取 / 请求格式抓包学习（Playwright）"""
import json
import os
import sys
import time

LOGIN_PAGE = "https://nicevoice.org/zh/ai-voice-cloning/"
CLONE_PAGE = "https://nicevoice.org/zh/ai-voice-cloning/"
API_HOST = "api.nicevoice.org"

if getattr(sys, "frozen", False):
    _BASE = os.path.join(os.path.expanduser("~"), ".voice-relay")
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(_BASE, "token.json")
CAPTURE_FILE = os.path.join(_BASE, "captured_request.json")


def save_token(storage_state_path=TOKEN_FILE):
    """由调用方传入 playwright 的 storage_state 已保存路径，这里只做占位"""
    pass


def _extract_token_from_storage(storage: dict):
    """从 localStorage 里找 token 样的值。返回 (key, value) 或 None。
    Supabase 类的值是 JSON 串（内含 access_token），会自动解包。"""
    import json as _json
    candidates = []
    for origin in storage.get("origins", []):
        for item in origin.get("localStorage", []):
            name, value = item.get("name", ""), item.get("value", "")
            ln = name.lower()
            if ("token" in ln or "authorization" in ln or ln.endswith("jwt")) and value:
                v = value
                # 值本身可能是 JSON 包着的：去引号 / 解 JSON 拿 access_token
                if v.startswith('"') and v.endswith('"'):
                    v = v[1:-1]
                if v.startswith("{"):
                    try:
                        obj = _json.loads(v)
                        if isinstance(obj, dict):
                            if obj.get("access_token"):
                                v = obj["access_token"]
                            elif obj.get("token"):
                                v = obj["token"]
                    except Exception:
                        pass
                if v:
                    candidates.append((name, v))
    # 优先 key 里直接叫 token 的
    candidates.sort(key=lambda kv: 0 if kv[0].lower() in ("token", "access_token", "user_token", "authorization") else 1)
    return candidates[0] if candidates else None


def _launch_browser(p, headless, log=print):
    """优先用系统已装浏览器（Windows 的 Edge、mac 的 Chrome），
    避免打包时把 Chromium 内核塞进安装包；都没有才回退到自带 Chromium。"""
    channels = ["msedge", "chrome"] if sys.platform.startswith("win") else ["chrome", "msedge"]
    for ch in channels:
        try:
            b = p.chromium.launch(headless=headless, channel=ch)
            log("已使用系统浏览器：%s" % ch)
            return b
        except Exception:
            continue
    try:
        b = p.chromium.launch(headless=headless)
        log("已使用内置浏览器内核")
        return b
    except Exception as e:
        raise RuntimeError(
            "找不到可用浏览器。请安装 Microsoft Edge 或 Google Chrome 后重试。（%s）" % e)


def login_and_save_token(headless=False, account="", password="", timeout=180, log=print):
    """打开网站让用户登录（或自动填账号密码），成功后把 cookies + localStorage 存到 token.json。

    返回 (token_str 或 None, storage_path)
    """
    from playwright.sync_api import sync_playwright

    log("打开浏览器，进入 %s" % LOGIN_PAGE)
    with sync_playwright() as p:
        browser = _launch_browser(p, headless, log=log)
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        page = ctx.new_page()
        page.goto(LOGIN_PAGE, wait_until="domcontentloaded", timeout=60000)

        # 尝试点出登录入口
        for sel in ["text=登录", "text=登 录", "text=Login", "text=Sign in", "[class*=login]"]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1500):
                    el.click()
                    log("已点击登录入口：%s" % sel)
                    break
            except Exception:
                continue

        # 自动填账号密码（填了才尝试）
        if account and password:
            try:
                page.wait_for_selector("input[type=password]", timeout=8000)
                pwd = page.locator("input[type=password]").first
                # 密码框前面的第一个可见文本输入框当账号框
                acc = page.locator(
                    "input:below(input[type=password]) >> input[type=text], input[type=email], input[type=tel]"
                ).first
                if not acc.count():
                    acc = page.locator("input[type=text], input[type=email], input[type=tel]").first
                acc.fill(account)
                pwd.fill(password)
                log("已自动填入账号密码")
                for sel in ["button:has-text('登录')", "button:has-text('登 录')", "text=登录"]:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=1000):
                            btn.click()
                            break
                    except Exception:
                        continue
            except Exception as e:
                log("自动填表失败（可手动在窗口里登录）：%s" % e)

        if headless:
            log("当前是无头模式：若登录页有验证码/扫码，请把配置里 login_headless 改为 false 重试")

        # 等待出现 token（登录成功标志）
        token_kv = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                storage = ctx.storage_state()
                token_kv = _extract_token_from_storage(storage)
                if token_kv:
                    break
            except Exception:
                pass
            time.sleep(2)

        if token_kv:
            storage = ctx.storage_state(path=TOKEN_FILE)
            log("登录成功，token 已保存（localStorage key: %s）" % token_kv[0])
            browser.close()
            return token_kv[1], TOKEN_FILE

        # 没等到 token，也把 state 存下来（可能有 cookie 鉴权）
        ctx.storage_state(path=TOKEN_FILE)
        log("超时未在 localStorage 找到 token，但已保存 cookies 到 token.json（若网站用 cookie 鉴权仍可用）")
        browser.close()
        return None, TOKEN_FILE


def capture_tts_request(headless=False, timeout=300, log=print):
    """抓包学习：打开配音页面，监听对 api.nicevoice.org 的请求，
    用户在页面里手动合成一次，工具记录 /clone2/tts 的真实请求格式。"""
    from playwright.sync_api import sync_playwright

    captured = []

    def on_request(req):
        if API_HOST in req.url:
            info = {
                "url": req.url,
                "method": req.method,
                "headers": {k: v for k, v in req.headers.items()
                            if k.lower() not in ("cookie", "content-length", "host")},
                "post_data": req.post_data,
            }
            captured.append(info)
            log("[抓到请求] %s %s" % (req.method, req.url))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        kwargs = {"storage_state": TOKEN_FILE} if os.path.exists(TOKEN_FILE) else {}
        ctx = browser.new_context(viewport={"width": 1280, "height": 860}, **kwargs)
        page = ctx.new_page()
        page.on("request", on_request)
        page.goto(CLONE_PAGE, wait_until="domcontentloaded", timeout=60000)
        log("请在打开的页面里手动合成一次配音（选中音色 → 输入文字 → 点生成）。最多等 %d 秒…" % timeout)

        deadline = time.time() + timeout
        target = None
        while time.time() < deadline:
            for c in captured:
                if "/tts" in c["url"] or "/clone" in c["url"]:
                    target = c
                    break
            if target:
                break
            time.sleep(1)

        if target:
            with open(CAPTURE_FILE, "w", encoding="utf-8") as f:
                json.dump(target, f, ensure_ascii=False, indent=2)
            log("已保存抓包结果到 captured_request.json")
            log("请求体预览：%s" % (target.get("post_data") or "(空)"))
        else:
            log("超时，没有抓到 TTS 请求。请确认页面上成功生成过一次音频。")
        browser.close()
    return target
