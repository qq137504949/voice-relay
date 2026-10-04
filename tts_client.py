"""TTS 客户端：nicevoice 真实协议
- 鉴权：x-token + x-account（来自网页 localStorage 的 user 键）
- 签名：x-sign = HMAC-SHA256(密钥, (ts + account + JSON.stringify(body)).lower()).hex()
- 流程：POST /clone2/tts {text, referenceId} -> taskSn -> 轮询 getItemByTaskSn -> audioUrl -> 下载
"""
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

import requests

if getattr(sys, "frozen", False):  # PyInstaller 打包后，数据放用户主目录
    BASE_DIR = os.path.join(os.path.expanduser("~"), ".voice-relay")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(BASE_DIR, exist_ok=True)
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")

API_BASE = "https://api.nicevoice.org"
TTS_URL = API_BASE + "/clone2/tts"
TASK_URL = API_BASE + "/clone2/getItemByTaskSn"
REF_LIST_URL = API_BASE + "/clone2/getRefAudioList"

SIGN_KEY = "9BSGc4rO5uSkAEDO1UaHur6fui5B5jJ4"

DEFAULT_CONFIG = {
    "proxy": "",                 # 留空=自动读系统代理；也可填 "127.0.0.1:7897" 或 "none" 强制直连
    "split_at_punctuation": True,   # 优先在标点处断句（强烈建议开启，关闭则纯按字符数切）
    "punct_lookahead": None,        # 为凑标点允许多看几个字；None=自动(1/4，最多8)，0=严格不超
    "request_interval": 1.0,
    "poll_interval": 2.0,
    "poll_timeout": 180,
}
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------- 鉴权状态 ----------

def _parse_user_store(raw):
    """localStorage 'user' 值：base64( urlencoded(json) ) -> {token, account, email}"""
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        obj = json.loads(urllib.parse.unquote(decoded))
        return {"token": obj.get("token", ""),
                "account": (obj.get("base") or {}).get("id", ""),
                "email": (obj.get("base") or {}).get("email", "")}
    except Exception:
        return None


def load_auth():
    """从 token.json 恢复 {token, account}"""
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            storage = json.load(f)
    except Exception:
        return None
    for origin in storage.get("origins", []):
        for item in origin.get("localStorage", []):
            if item.get("name") == "user" and item.get("value"):
                u = _parse_user_store(item["value"])
                if u and u.get("token") and u.get("account"):
                    return u
    return None


# ---------- 网络会话 ----------

# 本机常见代理软件的默认端口（Clash / Clash Verge / v2rayN / Surge / Fiddler 等）
COMMON_PROXY_PORTS = (7890, 7897, 7891, 10809, 10808, 1080, 2080, 8888, 6152, 33210)
_PROXY_TTL = 30.0  # 自动探测结果缓存时间（秒）
_proxy_cache = {"ts": 0.0, "value": None}


def _with_scheme(hostport, scheme="http"):
    hostport = str(hostport).strip()
    if "://" in hostport:
        return hostport
    return "%s://%s" % (scheme, hostport)


def _socks_ok():
    try:
        import socks  # noqa: F401  PySocks
        return True
    except Exception:
        return False


def _parse_win_registry_proxy():
    """Windows 系统代理（Internet Settings 注册表）。返回 (http, https) 或 None。"""
    try:
        import winreg
    except Exception:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            def q(name):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except FileNotFoundError:
                    return None
            if str(q("ProxyEnable") or "0") not in ("1", "True"):
                return None
            server = str(q("ProxyServer") or "").strip()
    except Exception:
        return None
    if not server:
        return None

    http_p = https_p = None
    if "=" in server:
        # 形如 http=127.0.0.1:7890;https=127.0.0.1:7891;socks=127.0.0.1:7892
        for part in server.split(";"):
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            key, val = key.strip().lower(), val.strip()
            if not val:
                continue
            if key == "http":
                http_p = _with_scheme(val)
            elif key == "https":
                https_p = _with_scheme(val)
            elif key == "socks":
                if _socks_ok():
                    http_p = https_p = _with_scheme(val, "socks5h")
    else:
        http_p = https_p = _with_scheme(server)

    if not http_p and not https_p:
        return None
    return (http_p or https_p, https_p or http_p)


def _parse_mac_proxy():
    """macOS 系统代理（scutil --proxy）。返回 (http, https) 或 None。"""
    try:
        r = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5)
        out = r.stdout
    except Exception:
        return None

    def pick(enable_key, host_key, port_key, scheme):
        en = re.search(enable_key + r"\s*:\s*(\d+)", out)
        host = re.search(host_key + r"\s*:\s*(\S+)", out)
        port = re.search(port_key + r"\s*:\s*(\d+)", out)
        if not (en and host and port) or en.group(1) != "1":
            return None
        return "%s://%s:%s" % (scheme, host.group(1), port.group(1))

    url = (pick("HTTPSEnable", "HTTPSProxy", "HTTPSPort", "http")
           or pick("HTTPEnable", "HTTPProxy", "HTTPPort", "http"))
    if not url:
        url = pick("SOCKSEnable", "SOCKSProxy", "SOCKSPort", "socks5h")
        if url and not _socks_ok():
            url = None
    return (url, url) if url else None


def _parse_env_proxy():
    """环境变量里的代理（Windows 上很常见）。"""
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        val = (os.environ.get(key) or "").strip()
        if val:
            url = _with_scheme(val)
            if url.startswith("socks") and not _socks_ok():
                continue
            return (url, url)
    return None


def _probe_local_proxy():
    """系统代理没开启时，探测本机常见代理端口（能连上就认为代理在跑）。"""
    for port in COMMON_PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                url = "http://127.0.0.1:%d" % port
                return (url, url)
        except Exception:
            continue
    return None


def _system_proxies(force_refresh=False):
    """解析代理，返回 {"http":..., "https":...}；None 表示直连。

    优先级（端口不写死，按当前环境自动判断）：
      1) config.json 的 "proxy" 字段（none/direct/off = 强制直连；可省略 scheme）
      2) 系统代理：Windows 注册表 / macOS scutil / 环境变量
      3) 本机常见代理端口探测（系统代理没开、但代理软件在运行）
      4) 都没有 -> 直连
    """
    manual = str(load_config().get("proxy") or "").strip()
    if manual:
        if manual.lower() in ("none", "direct", "off", "0"):
            return None
        url = _with_scheme(manual)
        return {"http": url, "https": url}

    now = time.time()
    if not force_refresh and now - _proxy_cache["ts"] < _PROXY_TTL:
        return _proxy_cache["value"]

    if sys.platform.startswith("win"):
        found = _parse_win_registry_proxy() or _parse_env_proxy()
    elif sys.platform == "darwin":
        found = _parse_mac_proxy() or _parse_env_proxy()
    else:
        found = _parse_env_proxy()
    if not found:
        found = _probe_local_proxy()

    value = {"http": found[0], "https": found[1]} if found else None
    _proxy_cache.update(ts=now, value=value)
    return value


def get_session(force_refresh=False):
    s = requests.Session()
    s.trust_env = False
    px = _system_proxies(force_refresh=force_refresh)
    if px:
        s.proxies.update(px)
    return s


def _describe_channel(session):
    return session.proxies.get("https") or "直连"


def _js_hex_key(key_str):
    """复刻前端 WMe()：按 parseInt(chunk, 16) 语义把字符串转字节数组。
    parseInt('SG',16)=NaN -> 0；parseInt('5u',16)=5。"""
    out = bytearray()
    for i in range(0, len(key_str), 2):
        chunk = key_str[i:i + 2]
        k = 0
        while k < len(chunk) and chunk[k] in "0123456789abcdefABCDEF":
            k += 1
        out.append(int(chunk[:k], 16) if k else 0)
    return bytes(out)


def _sign(ts_ms, account, body):
    msg = (str(ts_ms) + account + json.dumps(body, ensure_ascii=False, separators=(",", ":"))).lower()
    key = _js_hex_key(SIGN_KEY)
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _headers(auth, body):
    ts = int(time.time() * 1000)
    return {
        "x-os": "web",
        "x-appid": "10",
        "x-code": "116",
        "x-timezone-offset": "480",
        "x-ts": str(ts),
        "x-account": auth["account"],
        "x-token": auth["token"],
        "x-sign": _sign(ts, auth["account"], body),
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://nicevoice.org",
        "Referer": "https://nicevoice.org/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    }


def _friendly_net_error(e):
    """把网络异常翻译成可操作的提示。"""
    low = str(e).lower()
    if "proxy" in low or isinstance(e, requests.exceptions.ProxyError):
        return ("代理连接失败：%s\n"
                "请在 config.json 里设置正确的代理（例如 {\"proxy\": \"127.0.0.1:7897\"}），"
                "或填 {\"proxy\": \"none\"} 强制直连。" % e)
    if isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)) or "timed out" in low:
        return ("连接超时：%s\n"
                "通常是本机没走代理导致。请开启代理软件的「系统代理」，"
                "或在 config.json 里设置 {\"proxy\": \"127.0.0.1:7890\"}。" % e)
    return "网络异常：%s\n请检查网络连接或代理设置。" % e


def _post(session, auth, url, body, log=print):
    try:
        r = session.post(url, headers=_headers(auth, body), json=body, timeout=60)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(_friendly_net_error(e))
    if r.status_code in (401, 403):
        raise RuntimeError("登录态失效（HTTP %d），请重新点「登录保存 token」" % r.status_code)
    if r.status_code >= 500:
        raise RuntimeError("服务端错误 HTTP %d（可能瞬时故障，稍后重试）" % r.status_code)
    r.raise_for_status()
    try:
        data = r.json()
    except Exception:
        raise RuntimeError("响应非 JSON：%s" % r.text[:200])
    if data.get("code") != 200:
        code = data.get("code")
        if str(code) in ("200010003", "401", "403") or "登录" in str(data.get("msg", "")):
            raise RuntimeError("登录态失效（code=%s, msg=%s），请重新登录" % (code, data.get("msg")))
        raise RuntimeError("接口返回 code=%s msg=%s" % (code, data.get("msg")))
    return data.get("data") or {}


# ---------- 音色 ----------

def list_references(session=None, auth=None, log=print):
    """拉取克隆音色列表 [{referenceId, referenceName}]"""
    auth = auth or load_auth()
    if not auth:
        raise RuntimeError("未登录，请先点「登录保存 token」")
    s = session or get_session()
    if session is None:
        log("网络通道：%s" % _describe_channel(s))
    data = _post(s, auth, REF_LIST_URL, {}, log=log)
    return data.get("refList") or []


# ---------- 文本分割 ----------

# 句末标点：优先在这里断（分句最自然）
_SENT_END = "。！？!?；;…"
# 段末换行：最强断点
_PARA_END = "\n\r"
# 句内停顿：找不到句末标点时退而求其次（不要在词中间硬切）
_SENT_PAUSE = "，、：:,.'\"）)】]》」』"
_PUNCT = _SENT_END + _PARA_END + _SENT_PAUSE
# 标点优先级加分：越"像句末"越优先
_PUNCT_BONUS = {ch: 3 for ch in _SENT_END}
_PUNCT_BONUS.update({ch: 5 for ch in _PARA_END})


def _pick_cut(text, start, chunk_size, min_len, lookahead):
    """在 start 附近找一个最合适的切点（返回绝对索引，标点归前一段）。

    只在 [start+min_len, start+chunk_size+lookahead] 里挑，避免切出过短碎片；
    优先段落/句末标点，同类里取离目标长度最近的；没有标点则返回 None。
    """
    target = start + chunk_size
    lo = start + min_len
    hi = min(start + chunk_size + lookahead, len(text))
    best, best_score = None, None
    for k in range(lo, hi):
        ch = text[k]
        if ch not in _PUNCT:
            continue
        cut = k + 1                                   # 标点留给前一段，读起来才收得住
        score = abs(cut - target) - _PUNCT_BONUS.get(ch, 0)
        if best_score is None or score < best_score:
            best, best_score = cut, score
    return best


def split_text(text, chunk_size=20, prefer_punct=True, lookahead=None):
    """按字符数分段，但优先在标点处裁开，避免把句子/词切断。

    - 每段尽量不超过 chunk_size；为了凑到标点允许向后多看 lookahead 个字
    - 优先段落换行，其次句末标点（。！？；），再次句内停顿（，、：）
    - 标点跟着前一段，下一段从标点之后开始
    - 窗口中确实没有标点时，才退回按字符数硬切
    """
    text = text.strip()
    if not text:
        return []
    chunk_size = max(1, int(chunk_size))
    n = len(text)

    if not prefer_punct:
        return [text[i:i + chunk_size].strip()
                for i in range(0, n, chunk_size) if text[i:i + chunk_size].strip()]

    min_len = max(1, chunk_size // 2)                 # 最短一段，防止碎片化
    if lookahead is None:
        lookahead = max(1, min(chunk_size // 4, 8))   # 默认允许略微超出以凑标点
    lookahead = max(0, int(lookahead))

    chunks = []
    i = 0
    while i < n:
        if n - i <= chunk_size:                       # 剩下的不超一段，直接收尾
            chunks.append(text[i:])
            break
        cut = _pick_cut(text, i, chunk_size, min_len, lookahead) or (i + chunk_size)
        chunks.append(text[i:cut])
        i = cut
    return [c.strip() for c in chunks if c.strip()]


# ---------- 单段合成 ----------

def _tts_one(session, auth, text, reference_id, poll_interval, poll_timeout, log=print):
    data = _post(session, auth, TTS_URL, {"text": text, "referenceId": reference_id}, log=log)
    task_sn = data.get("taskSn")
    if not task_sn:
        raise RuntimeError("未返回 taskSn：%s" % data)
    log("    taskSn=%s，轮询结果…" % task_sn)

    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        d = _post(session, auth, TASK_URL, {"taskSn": task_sn}, log=log)
        status = d.get("status")
        if status == 2:
            url = d.get("audioUrl") or d.get("audioUrl2")
            if not url:
                raise RuntimeError("合成完成但没有音频地址：%s" % d)
            return url
        if status == 3:
            raise RuntimeError("合成失败：%s" % d.get("statusStr"))
        # status 1 = 处理中，继续等
    raise RuntimeError("轮询超时（%ds），taskSn=%s" % (poll_timeout, task_sn))


# ---------- MP3 合并 ----------

def merge_mp3(parts, out_path, log=print):
    tmpdir = tempfile.mkdtemp(prefix="voice_relay_")
    try:
        paths = []
        for idx, data in enumerate(parts):
            p = os.path.join(tmpdir, "part_%03d.mp3" % idx)
            with open(p, "wb") as f:
                f.write(data)
            paths.append(p)

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            list_file = os.path.join(tmpdir, "list.txt")
            with open(list_file, "w", encoding="utf-8") as f:
                for p in paths:
                    f.write("file '%s'\n" % p.replace("'", "'\\''"))
            r = subprocess.run(
                [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
                 "-c:a", "libmp3lame", "-q:a", "2", out_path],
                capture_output=True, text=True)
            if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                log("ffmpeg 合并完成")
                return out_path
            log("ffmpeg 合并失败，回退为直接拼接")

        with open(out_path, "wb") as out:
            for p in paths:
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, out)
        log("直接拼接合并完成")
        return out_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 主流程 ----------

def synthesize(text, chunk_size, reference_id, out_path, log=print, should_stop=None):
    cfg = load_config()
    auth = load_auth()
    if not auth:
        raise RuntimeError("未登录：请先点「登录保存 token」")

    session = get_session()
    log("网络通道：%s" % _describe_channel(session))
    log("账号：%s" % auth.get("email", auth["account"]))

    if not reference_id:
        log("未填音色ID，自动获取第一个克隆音色…")
        refs = list_references(session, auth, log=log)
        if not refs:
            raise RuntimeError("账号下没有克隆音色，请先在网站上克隆一个")
        reference_id = refs[0]["referenceId"]
        log("使用音色：%s（%s）" % (refs[0].get("referenceName"), reference_id))

    chunks = split_text(text, chunk_size, cfg.get("split_at_punctuation", True),
                        cfg.get("punct_lookahead"))
    total = len(chunks)
    log("文本共 %d 字，分割为 %d 段（每段 %d 字符）" % (len(text.strip()), total, chunk_size))
    if total == 0:
        raise RuntimeError("输入文本为空")

    parts = []
    for idx, chunk in enumerate(chunks, 1):
        if should_stop and should_stop():
            log("已停止")
            return None
        log("[%d/%d] %r" % (idx, total, chunk[:30] + ("…" if len(chunk) > 30 else "")))
        url = _tts_one(session, auth, chunk, reference_id,
                       float(cfg.get("poll_interval", 2.0)),
                       int(cfg.get("poll_timeout", 180)), log=log)
        try:
            r = session.get(url, timeout=120)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise RuntimeError("下载音频失败：%s" % _friendly_net_error(e))
        audio = r.content
        if len(audio) < 512:
            log("警告：第 %d 段音频只有 %d 字节，可能为空" % (idx, len(audio)))
        parts.append(audio)
        log("[%d/%d] 完成，%d 字节" % (idx, total, len(audio)))

        if idx < total and cfg.get("request_interval"):
            time.sleep(float(cfg["request_interval"]))

    merge_mp3(parts, out_path, log=log)
    log("已保存：%s" % out_path)
    return out_path
