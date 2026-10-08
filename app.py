#!/usr/bin/env python3
"""配音中转站 GUI：长文本 → 分段调用 nicevoice 接口 → 合并成一个音频文件（wav / mp3）"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import scrolledtext, ttk, filedialog, messagebox

import sys

import tts_client
from tts_client import synthesize
import browser_login

if getattr(sys, "frozen", False):
    BASE = tts_client.BASE_DIR
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(BASE, "output")


class App:
    def __init__(self, root):
        self.root = root
        root.title("配音中转站 - nicevoice")
        root.geometry("760x720")
        self.log_q = queue.Queue()
        self.worker = None
        self.stop_flag = threading.Event()
        self._build_ui()
        self.root.after(100, self._drain_log)
        self._log("就绪。音色下拉框可切换（点「刷新音色」加载）；token 失效时重新点「登录保存 token」。")
        self.root.after(400, self.do_env_check)
        self.root.after(500, self.do_load_voices)

    # ---------- UI ----------
    def _build_ui(self):
        frm = ttk.Frame(self.root, padding=8)
        frm.pack(fill="both", expand=True)

        row1 = ttk.Frame(frm)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="分割字符数:").pack(side="left")
        self.chunk_var = tk.StringVar(value="20")
        ttk.Spinbox(row1, from_=1, to=500, width=6, textvariable=self.chunk_var).pack(side="left", padx=(2, 12))
        ttk.Label(row1, text="音色:").pack(side="left")
        self.voice_combo = ttk.Combobox(row1, state="readonly", width=30)
        self.voice_combo.pack(side="left", padx=(2, 4))
        self.voice_map = {}  # 显示名 -> referenceId
        ttk.Button(row1, text="刷新音色", command=self.do_load_voices).pack(side="left", padx=2)
        ttk.Label(row1, text="格式:").pack(side="left", padx=(10, 0))
        self.fmt_var = tk.StringVar(value=str(tts_client.load_config().get("output_format", "wav")).lower())
        ttk.Combobox(row1, state="readonly", width=5, textvariable=self.fmt_var,
                     values=("wav", "mp3")).pack(side="left", padx=2)

        row2 = ttk.Frame(frm)
        row2.pack(fill="x", pady=4)
        ttk.Button(row2, text="登录保存 token", command=self.do_login).pack(side="left", padx=2)
        self.token_label = ttk.Label(row2, text="token: 未登录", foreground="#888")
        self.token_label.pack(side="left", padx=8)

        ttk.Label(frm, text="长文本（不限字数）:").pack(anchor="w", pady=(6, 0))
        self.text_box = scrolledtext.ScrolledText(frm, height=10, wrap="word", font=("", 12))
        self.text_box.pack(fill="both", expand=True, pady=2)

        row3 = ttk.Frame(frm)
        row3.pack(fill="x", pady=4)
        self.start_btn = ttk.Button(row3, text="开始生成", command=self.do_start)
        self.start_btn.pack(side="left")
        ttk.Button(row3, text="停止", command=self.do_stop).pack(side="left", padx=6)
        ttk.Button(row3, text="打开生成目录", command=self.do_open_output).pack(side="left", padx=6)
        ttk.Button(row3, text="环境自检", command=self.do_env_check).pack(side="left", padx=6)
        self.progress = ttk.Label(row3, text="")
        self.progress.pack(side="left", padx=10)

        ttk.Label(frm, text="日志:").pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(frm, height=12, wrap="word", state="disabled",
                                                 font=("Menlo", 11))
        self.log_box.pack(fill="both", expand=True)

        self._refresh_token_label()

    def _log(self, msg):
        self.log_q.put(msg)

    def _drain_log(self):
        try:
            while True:
                line = self.log_q.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", line + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._drain_log)

    def _refresh_token_label(self):
        if tts_client.load_auth():
            auth = tts_client.load_auth()
            self.token_label.configure(text="已登录: %s" % auth.get("email", auth["account"][:8]),
                                       foreground="green")
        else:
            self.token_label.configure(text="token: 未登录", foreground="#888")

    # ---------- 动作 ----------
    def do_load_voices(self):
        def job():
            try:
                refs = tts_client.list_references(log=self._log)
                if not refs:
                    self._log("账号下没有克隆音色，请先在网站上克隆")
                    return
                self.voice_map = {}
                names = []
                for r in refs:
                    name = "%s（%ss）" % (r.get("referenceName", r["referenceId"]), r.get("audioDuration", "?"))
                    names.append(name)
                    self.voice_map[name] = r["referenceId"]
                self.root.after(0, lambda: (
                    self.voice_combo.configure(values=names),
                    self.voice_combo.current(0)))
                self._log("已加载 %d 个音色" % len(refs))
            except Exception as e:
                self._log("拉取音色失败：%s" % e)
        threading.Thread(target=job, daemon=True).start()

    def do_login(self):
        def job():
            try:
                cfg = tts_client.load_config()
                token, _ = browser_login.login_and_save_token(
                    headless=cfg.get("login_headless", False),
                    log=self._log)
                if token:
                    self._log("登录态已保存到本地 token.json")
                self.root.after(0, self._refresh_token_label)
            except Exception as e:
                self._log("登录失败：%s" % e)
        threading.Thread(target=job, daemon=True).start()

    def do_start(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "任务进行中，请先停止")
            return
        text = self.text_box.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("提示", "请先输入文本")
            return
        try:
            chunk = max(1, int(self.chunk_var.get()))
        except ValueError:
            messagebox.showwarning("提示", "分割字符数必须是数字")
            return

        os.makedirs(DEFAULT_OUT, exist_ok=True)
        fmt = (self.fmt_var.get() or "wav").lower().lstrip(".")
        out_path = os.path.join(DEFAULT_OUT, "配音_%s.%s" % (time.strftime("%H%M%S"), fmt))
        try:  # 记住格式选择
            cfg = tts_client.load_config()
            cfg["output_format"] = fmt
            tts_client.save_config(cfg)
        except Exception:
            pass

        self.stop_flag.clear()
        self.start_btn.configure(state="disabled")

        def job():
            try:
                reference_id = self.voice_map.get(self.voice_combo.get(), "")
                result = synthesize(text, chunk, reference_id,
                                    out_path, log=self._log, should_stop=self.stop_flag.is_set)
                if result:
                    self.root.after(0, lambda: messagebox.showinfo("完成", "已生成：\n%s" % result))
            except Exception as e:
                self._log("出错：%s" % e)
                msg = str(e)
                self.root.after(0, lambda m=msg: messagebox.showerror("出错", m))
            finally:
                self.root.after(0, lambda: self.start_btn.configure(state="normal"))

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def do_stop(self):
        self.stop_flag.set()
        self._log("等待当前段完成…")

    def do_open_output(self):
        os.makedirs(DEFAULT_OUT, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(DEFAULT_OUT)  # noqa: S606 - Windows 资源管理器
        elif sys.platform == "darwin":
            subprocess.Popen(["open", DEFAULT_OUT])
        else:
            subprocess.Popen(["xdg-open", DEFAULT_OUT])

    def do_env_check(self):
        """打印音频环境自检：miniaudio / _cffi_backend / ffmpeg 是否就绪。"""
        def job():
            self._log("----- 环境自检 -----")
            for line in tts_client.audio_env_report().splitlines():
                self._log(line)
            self._log("--------------------")
        threading.Thread(target=job, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
