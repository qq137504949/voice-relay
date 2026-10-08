#!/usr/bin/env python3
"""GitHub Actions artifact 多线程分段下载器。

为什么要用它：从国内直连 GitHub 单连接大约只有 50KB/s，但多连接并行能跑到
500KB/s 以上（是单连接限速，不是带宽不够）。这个脚本把每个分段丢进一个线程，
最后拼接成完整文件。

用法：
    python fast_download.py <artifact_id> <输出路径> [--threads 16] [--repo owner/repo]
token 取 `gh auth token` 的输出，或用环境变量 GH_TOKEN。
"""
import argparse
import os
import subprocess
import sys
import threading
import time

import requests

# 下载时不要走系统/环境变量里的代理（代理常常更慢或直接 502）
SESSION = requests.Session()
SESSION.trust_env = False


def get_token():
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip()
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except Exception:
        sys.exit("找不到 GitHub token：请先 gh auth login，或设置 GH_TOKEN 环境变量")


def resolve_url(api_url, token):
    """跟随后端跳转，拿到真正可下载的签名 URL 和文件大小。"""
    r = SESSION.get(api_url, headers={"Authorization": "Bearer " + token},
                    allow_redirects=True, stream=True, timeout=60)
    r.raise_for_status()
    size = int(r.headers.get("Content-Length") or 0)
    r.close()
    return r.url, size


def download(url, out_path, threads=16, token=None):
    # 跳转后的签名地址（Azure Blob）不认 GitHub token，带了反而 401
    headers = {}
    probe = SESSION.get(url, headers={}, stream=True, timeout=60)
    probe.raise_for_status()
    total = int(probe.headers.get("Content-Length") or 0)
    probe.close()
    if total <= 0:
        # 不支持 HEAD/Range 时退化成单线程
        with SESSION.get(url, headers=headers, stream=True, timeout=600) as r, open(out_path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
        return total

    part = total // threads
    ranges = []
    for i in range(threads):
        start = i * part
        end = total - 1 if i == threads - 1 else (i + 1) * part - 1
        ranges.append((i, start, end))

    tmpdir = out_path + ".parts"
    os.makedirs(tmpdir, exist_ok=True)
    done = [0] * threads
    lock = threading.Lock()
    errors = []

    def worker(idx, start, end):
        p = os.path.join(tmpdir, "%03d" % idx)
        got = os.path.getsize(p) if os.path.exists(p) else 0
        for attempt in range(12):
            try:
                s = start + got
                if got and s > end:
                    break
                h = dict(headers)
                h["Range"] = "bytes=%d-%d" % (s, end)
                mode = "ab" if got else "wb"
                with SESSION.get(url, headers=h, stream=True, timeout=(30, 120)) as r:
                    if r.status_code not in (200, 206):
                        raise RuntimeError("HTTP %s" % r.status_code)
                    with open(p, mode) as f:
                        for chunk in r.iter_content(1 << 16):
                            if not chunk:
                                continue
                            f.write(chunk)
                got = os.path.getsize(p)
                if got >= end - start + 1:
                    return
            except Exception as e:  # 断流就续传重试
                errors.append(str(e))
                got = os.path.getsize(p) if os.path.exists(p) else 0
                time.sleep(min(2 * (attempt + 1), 10))
        if got < end - start + 1:
            errors.append("分段 %d 未完成（%d/%d 字节）" % (idx, got, end - start + 1))

    def reporter():
        while any(t.is_alive() for t in ts):
            cur = 0
            for i, _, _ in ranges:
                try:
                    cur += os.path.getsize(os.path.join(tmpdir, "%03d" % i))
                except OSError:
                    pass
            pct = cur * 100.0 / total
            speed = cur / max(1e-6, time.time() - t0)
            sys.stdout.write("\r  %5.1f%%  %6.2f MB / %.2f MB  %.0f KB/s   " %
                             (pct, cur / 1e6, total / 1e6, speed / 1024))
            sys.stdout.flush()
            time.sleep(1)

    t0 = time.time()
    ts = [threading.Thread(target=worker, args=r) for r in ranges]
    for t in ts:
        t.start()
    rep = threading.Thread(target=reporter, daemon=True)
    rep.start()
    for t in ts:
        t.join()
    print()

    if errors:
        print("  警告：%d 次分段重试（%s）" % (len(errors), errors[0]))

    # 拼接前先严格校验每个分段，避免拼出个残文件
    short = []
    for i, start, end in ranges:
        p = os.path.join(tmpdir, "%03d" % i)
        want = end - start + 1
        have = os.path.getsize(p) if os.path.exists(p) else 0
        if have != want:
            short.append("分段 %d: %d/%d" % (i, have, want))
    if short:
        sys.exit("下载不完整，已中止（%s）。重跑本命令会自动续传。" % "; ".join(short[:5]))

    # 拼接
    with open(out_path, "wb") as out:
        for i, _, _ in ranges:
            p = os.path.join(tmpdir, "%03d" % i)
            with open(p, "rb") as f:
                while True:
                    buf = f.read(1 << 20)
                    if not buf:
                        break
                    out.write(buf)
    size = os.path.getsize(out_path)
    if size != total:
        sys.exit("拼接后大小不符：%d != %d" % (size, total))
    print("  完成：%s（%.1f MB，耗时 %.0f 秒）" % (out_path, size / 1e6, time.time() - t0))
    return size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact_id")
    ap.add_argument("out_path")
    ap.add_argument("--repo", default=None, help="owner/repo，不传则从 git remote 推断")
    ap.add_argument("--threads", type=int, default=16)
    args = ap.parse_args()

    token = get_token()
    repo = args.repo
    if not repo:
        try:
            url = subprocess.check_output(["git", "remote", "get-url", "origin"], text=True).strip()
            repo = url.split("github.com")[-1].strip("/:").replace(".git", "")
        except Exception:
            sys.exit("无法推断仓库，请用 --repo owner/name")
    api = "https://api.github.com/repos/%s/actions/artifacts/%s/zip" % (repo, args.artifact_id)
    print("解析下载地址：%s" % api)
    final_url, size = resolve_url(api, token)
    print("文件大小：%.1f MB" % (size / 1e6))
    download(final_url, args.out_path, threads=args.threads, token=token)


if __name__ == "__main__":
    main()
