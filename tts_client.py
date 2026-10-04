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
    "split_at_punctuation": True,
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

def _system_proxies():
    """解析代理设置。端口不写死，按当前环境自动判断。

    优先级：
      1) config.json 里的 "proxy"（手动指定；填 none/direct/off 表示强制直连）
      2) macOS 系统代理：HTTPS -> HTTP -> SOCKS（端口是多少就用多少）
      3) 都没有 -> 返回 None，直接连
    """
    manual = str(load_config().get("proxy") or "").strip()
    if manual:
        if manual.lower() in ("none", "direct", "off", "0"):
            return None
        if "://" not in manual:
            manual = "http://" + manual
        return {"http": manual, "https": manual}

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
    if url:
        return {"http": url, "https": url}

    socks = pick("SOCKSEnable", "SOCKSProxy", "SOCKSPort", "socks5h")
    if socks:
        try:
            import socks  # noqa: F401  需要 PySocks 才能走 SOCKS
            return {"http": socks, "https": socks}
        except Exception:
            pass
    return None


def get_session():
    s = requests.Session()
    s.trust_env = False
    px = _system_proxies()
    if px:
        s.proxies.update(px)
    return s


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


def _post(session, auth, url, body, log=print):
    r = session.post(url, headers=_headers(auth, body), json=body, timeout=60)
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
    data = _post(s, auth, REF_LIST_URL, {}, log=log)
    return data.get("refList") or []


# ---------- 文本分割 ----------

_PUNCT = "。！？；!?;\n"


def split_text(text, chunk_size=20, prefer_punct=True):
    text = text.strip()
    if not text:
        return []
    chunks = []
    i, n = 0, len(text)
    while i < n:
        if n - i <= chunk_size:
            chunks.append(text[i:])
            break
        piece = text[i:i + chunk_size]
        cut = chunk_size
        if prefer_punct:
            for j in range(chunk_size - 1, max(chunk_size // 3, 1) - 1, -1):
                if piece[j - 1] in _PUNCT:
                    cut = j
                    break
        chunks.append(text[i:i + cut])
        i += cut
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
    px = session.proxies.get("https")
    log("网络通道：%s" % (px if px else "直连"))
    log("账号：%s" % auth.get("email", auth["account"]))

    if not reference_id:
        log("未填音色ID，自动获取第一个克隆音色…")
        refs = list_references(session, auth, log=log)
        if not refs:
            raise RuntimeError("账号下没有克隆音色，请先在网站上克隆一个")
        reference_id = refs[0]["referenceId"]
        log("使用音色：%s（%s）" % (refs[0].get("referenceName"), reference_id))

    chunks = split_text(text, chunk_size, cfg.get("split_at_punctuation", True))
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
        r = session.get(url, timeout=120)
        r.raise_for_status()
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
