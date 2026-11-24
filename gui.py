#!/usr/bin/env python3
"""
Claudian Bridge GUI — 轻量模型名替换代理（带图形界面）
"""
import re
import json
import sys
import os
import http.server
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import requests

CONFIG_FILE = os.path.join(os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(__file__), "config.json")

DEFAULTS = {
    "port": 15722,
    "target": "http://127.0.0.1:15721",
    "model": "step-3.7-flash",
}


class ProxyServer:
    def __init__(self, port, target, model, log_callback):
        self.port = port
        self.target = target.rstrip("/")
        self.model = model
        self.log = log_callback
        self.server = None
        self.thread = None
        self.pattern = re.compile(r'"model"\s*:\s*"[^"]*"')

    def _make_handler(self):
        pattern = self.pattern
        target = self.target
        replacement = self.model
        log = self.log

        class Handler(http.server.BaseHTTPRequestHandler):
            def _forward(self2, method):
                try:
                    path = self2.path
                    cl = int(self2.headers.get("Content-Length", 0))
                    body = self2.rfile.read(cl) if cl else b""
                    body_str = body.decode("utf-8", errors="replace")
                    found = pattern.findall(body_str)
                    if found:
                        body_str = pattern.sub(f'"model": "{replacement}"', body_str)
                        log(f"[替换] {set(found)} → {replacement}")
                    body_bytes = body_str.encode("utf-8")
                    headers = {k: v for k, v in self2.headers.items() if k.lower() not in ("host", "content-length", "connection")}

                    resp = requests.request(method=method, url=f"{target}{path}", headers=headers, data=body_bytes, stream=True, timeout=300)
                    self2.send_response(resp.status_code)
                    for k, v in resp.headers.items():
                        if k.lower() not in ("transfer-encoding", "content-encoding", "connection"):
                            self2.send_header(k, v)
                    self2.end_headers()
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            self2.wfile.write(chunk)
                            self2.wfile.flush()
                except Exception as e:
                    log(f"[错误] {e}")
                    try:
                        self2.send_response(502)
                        self2.end_headers()
                    except:
                        pass

            def do_POST(self2): self2._forward("POST")
            def do_GET(self2): self2._forward("GET")
            def do_OPTIONS(self2): self2._forward("OPTIONS")
            def log_message(self2, *args): pass

        return Handler

    def start(self):
        self.server = http.server.HTTPServer(("127.0.0.1", self.port), self._make_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.log(f"[启动] 监听 127.0.0.1:{self.port}，模型 claude-* → {self.model}")

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server = None
            self.log("[停止] 代理已关闭")


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Claudian Bridge")
        self.root.geometry("520x420")
        self.root.minsize(460, 360)
        self.root.resizable(True, True)
        
        # 居中
        self.root.update_idletasks()
        w, h = 520, 420
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.proxy: ProxyServer = None
        self._build_ui()
        self._load_config()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # 标题栏
        header = tk.Frame(self.root, bg="#1a1a2e", height=48)
        header.pack(fill="x")
        tk.Label(header, text="Claudian Bridge", font=("Microsoft YaHei", 13, "bold"), fg="#e94560", bg="#1a1a2e").pack(side="left", padx=16, pady=10)
        self.status_dot = tk.Canvas(header, width=12, height=12, bg="#1a1a2e", highlightthickness=0)
        self.status_dot.pack(side="right", padx=16, pady=12)
        self._draw_dot("gray")

        # 配置区
        cfg = tk.LabelFrame(self.root, text="配置", font=("Microsoft YaHei", 9), padx=10, pady=8)
        cfg.pack(fill="x", padx=12, pady=(10, 4))

        for i, (k, v) in enumerate([("端口", "port"), ("上游地址", "target"), ("目标模型", "model")]):
            tk.Label(cfg, text=k + ":", font=("Microsoft YaHei", 9)).grid(row=i, column=0, sticky="w", pady=3)
            entry = tk.Entry(cfg, width=52, font=("Consolas", 9))
            entry.grid(row=i, column=1, padx=8, pady=3, sticky="ew")
            setattr(self, f"e_{v}", entry)
        cfg.columnconfigure(1, weight=1)

        # 按钮
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill="x", padx=12, pady=(2, 4))

        self.btn_start = tk.Button(btn_frame, text="▶ 启动", command=self._start, font=("Microsoft YaHei", 10, "bold"), bg="#e94560", fg="white", activebackground="#c23152", relief="flat", cursor="hand2", width=10)
        self.btn_start.pack(side="left", padx=(0, 6))
        
        self.btn_stop = tk.Button(btn_frame, text="■ 停止", command=self._stop, font=("Microsoft YaHei", 10), bg="#555", fg="white", activebackground="#444", relief="flat", cursor="hand2", width=10, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 6))

        tk.Button(btn_frame, text="💾 保存配置", command=self._save_config, font=("Microsoft YaHei", 9), relief="flat", cursor="hand2").pack(side="left", padx=4)

        # 日志区
        log_frame = tk.LabelFrame(self.root, text="日志", font=("Microsoft YaHei", 9), padx=8, pady=6)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        self.log_area = scrolledtext.ScrolledText(log_frame, font=("Consolas", 9), bg="#0d1117", fg="#c9d1d9", insertbackground="white", relief="flat", state="disabled")
        self.log_area.pack(fill="both", expand=True)

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 12, 12, fill=color, outline="")

    def _log(self, msg):
        self.log_area.config(state="normal")
        self.log_area.insert("end", msg + "\n")
        self.log_area.see("end")
        self.log_area.config(state="normal" if self.log_area.cget("state") == "disabled" else "normal")
        # 重新设置为 disabled 以便后续只读
        self.log_area.config(state="disabled")

    def _load_config(self):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
        except:
            cfg = {}
        for k in DEFAULTS:
            getattr(self, f"e_{k}").insert(0, str(cfg.get(k, DEFAULTS[k])))

    def _save_config(self):
        cfg = {}
        for k in DEFAULTS:
            try:
                cfg[k] = type(DEFAULTS[k])(getattr(self, f"e_{k}").get())
            except:
                cfg[k] = getattr(self, f"e_{k}").get()
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
        self._log("[配置] 已保存")

    def _start(self):
        if self.proxy:
            return
        try:
            port = int(self.e_port.get())
        except:
            messagebox.showerror("错误", "端口必须是数字")
            return
        target = self.e_target.get().strip()
        model = self.e_model.get().strip()
        if not target or not model:
            messagebox.showerror("错误", "上游地址和目标模型不能为空")
            return

        self.proxy = ProxyServer(port, target, model, self._log)
        try:
            self.proxy.start()
        except Exception as e:
            self._log(f"[错误] 启动失败: {e}")
            self.proxy = None
            return

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.e_port.config(state="readonly")
        self.e_target.config(state="readonly")
        self.e_model.config(state="readonly")
        self._draw_dot("#00ff88")

    def _stop(self):
        if self.proxy:
            self.proxy.stop()
            self.proxy = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.e_port.config(state="normal")
        self.e_target.config(state="normal")
        self.e_model.config(state="normal")
        self._draw_dot("gray")

    def _on_close(self):
        if self.proxy:
            if messagebox.askyesno("确认", "代理正在运行，确定退出？"):
                self._stop()
                self.root.destroy()
        else:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
