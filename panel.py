#!/usr/bin/env python3
"""
Claudian Bridge Panel v1.0
完整代理控制面板：启动/停止/监控 proxy.py，实时日志 + 健康状态 + 统计
无需额外依赖，纯 tkinter + ttk，暗黑主题
"""

import subprocess
import threading
import time
import json
import os
import sys
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

# ── 配置 ──────────────────────────────────────────────
PROXY_SCRIPT = r"D:\claudian-bridge\proxy.py"
PROXY_PORT = 15722
HEALTH_URL = f"http://127.0.0.1:{PROXY_PORT}/health"
STATS_URL = f"http://127.0.0.1:{PROXY_PORT}/stats"
POLL_INTERVAL = 3000  # ms
LOG_TAIL_LINES = 50

# ── 颜色调色板 ─────────────────────────────────────────
CLR = {
    "bg":        "#0d1117",
    "bg2":       "#161b22",
    "bg3":       "#21262d",
    "border":    "#30363d",
    "text":      "#c9d1d9",
    "text2":     "#8b949e",
    "accent":    "#58a6ff",
    "accent2":   "#1f6feb",
    "green":     "#3fb950",
    "red":       "#f85149",
    "yellow":    "#d2991d",
    "purple":    "#a371f7",
    "orange":    "#db6d28",
}

# ── 工具函数 ──────────────────────────────────────────
def http_get(url, timeout=3):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None

def find_proxy_pid():
    """通过端口查找 proxy.py 进程 PID"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-NetTCPConnection -LocalPort 15722 -State Listen -ErrorAction SilentlyContinue).OwningProcess"],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW
        )
        pid_str = result.stdout.strip()
        if pid_str and pid_str.isdigit():
            return int(pid_str)
    except Exception:
        pass
    return None


# ── 面板主类 ──────────────────────────────────────────
class Panel:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Claudian Bridge Panel")
        self.root.geometry("680x540")
        self.root.minsize(560, 400)
        self.root.configure(bg=CLR["bg"])
        self.root.resizable(True, True)

        # 居中
        self.root.update_idletasks()
        w, h = 680, 540
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # 样式
        self._setup_style()

        self.proxy_process = None
        self.monitoring = False
        self.log_lines = []

        self._build_ui()
        self._start_monitor()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")

        # 全局
        style.configure(".", background=CLR["bg"], foreground=CLR["text"],
                        fieldbackground=CLR["bg2"], borderwidth=0, font=("Segoe UI", 9))
        style.configure("TFrame", background=CLR["bg"])
        style.configure("TLabel", background=CLR["bg"], foreground=CLR["text"])
        style.configure("TButton", background=CLR["bg3"], foreground=CLR["text"],
                        borderwidth=1, focusthickness=0, padding=(14, 5), font=("Segoe UI", 9, "bold"))
        style.map("TButton",
                  background=[("active", CLR["accent2"]), ("!disabled", CLR["bg3"])],
                  foreground=[("active", "#ffffff")])

        # 定制按钮样式
        style.configure("Start.TButton", background=CLR["green"], foreground="#000000")
        style.map("Start.TButton", background=[("active", "#2ea043")])
        style.configure("Stop.TButton", background=CLR["red"], foreground="#ffffff")
        style.map("Stop.TButton", background=[("active", "#da3633")])
        style.configure("Restart.TButton", background=CLR["orange"], foreground="#000000")
        style.map("Restart.TButton", background=[("active", "#bd561d")])
        style.configure("Secondary.TButton", background=CLR["bg3"], foreground=CLR["text"])
        style.map("Secondary.TButton", background=[("active", CLR["accent2"])])

        # 标签框
        style.configure("Card.TLabelframe", background=CLR["bg2"], bordercolor=CLR["border"],
                        relief="solid", borderwidth=1)
        style.configure("Card.TLabelframe.Label", background=CLR["bg2"], foreground=CLR["accent"],
                        font=("Segoe UI", 10, "bold"))

    def _build_ui(self):
        # ═══ 顶部标题栏 ═══
        header = tk.Frame(self.root, bg=CLR["bg2"], height=52)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(header, text="◈  Claudian Bridge", font=("Segoe UI", 15, "bold"),
                 fg=CLR["accent"], bg=CLR["bg2"]).pack(side="left", padx=18, pady=10)

        right_frame = tk.Frame(header, bg=CLR["bg2"])
        right_frame.pack(side="right", padx=18, pady=10)

        self.status_dot = tk.Canvas(right_frame, width=14, height=14, bg=CLR["bg2"], highlightthickness=0)
        self.status_dot.pack(side="right", padx=(8, 0))
        self._draw_dot(CLR["text2"])

        self.status_label = tk.Label(right_frame, text="检查中…", font=("Segoe UI", 9),
                                      fg=CLR["text2"], bg=CLR["bg2"])
        self.status_label.pack(side="right")

        # ═══ 控制按钮行 ═══
        ctrl = tk.Frame(self.root, bg=CLR["bg"], pady=10)
        ctrl.pack(fill="x", padx=16)

        self.btn_start = ttk.Button(ctrl, text="▶  启动代理", style="Start.TButton",
                                     command=self._start, width=14)
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_stop = ttk.Button(ctrl, text="■  停止代理", style="Stop.TButton",
                                    command=self._stop, width=14, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 8))

        self.btn_restart = ttk.Button(ctrl, text="↻  重启", style="Restart.TButton",
                                       command=self._restart, width=10)
        self.btn_restart.pack(side="left", padx=(0, 8))

        ttk.Button(ctrl, text="⚙  配置", style="Secondary.TButton",
                   command=self._open_config, width=10).pack(side="left", padx=(0, 8))

        ttk.Button(ctrl, text="📊  统计", style="Secondary.TButton",
                   command=self._show_stats, width=10).pack(side="left")

        # ═══ 状态卡片 ═══
        cards = tk.Frame(self.root, bg=CLR["bg"])
        cards.pack(fill="x", padx=16, pady=(4, 6))

        self._make_stat_card(cards, "总请求", "0", 0)
        self._make_stat_card(cards, "端口", "15722", 1)
        self._make_stat_card(cards, "模型", "step-3.7-flash", 2)
        self._make_stat_card(cards, "运行时长", "--", 3)

        # ═══ 日志区域 ═══
        log_frame = tk.LabelFrame(self.root, text="  实时日志  ", font=("Segoe UI", 10, "bold"),
                                   fg=CLR["accent"], bg=CLR["bg2"],
                                   relief="solid", bd=1, highlightbackground=CLR["border"])
        log_frame.pack(fill="both", expand=True, padx=16, pady=(4, 12))

        self.log_area = scrolledtext.ScrolledText(
            log_frame, font=("Cascadia Code", 9), bg=CLR["bg"], fg=CLR["text"],
            insertbackground="white", relief="flat", borderwidth=0,
            padx=10, pady=8, state="disabled", wrap="word"
        )
        self.log_area.pack(fill="both", expand=True)

        # 日志底部操作栏
        log_bar = tk.Frame(self.root, bg=CLR["bg"])
        log_bar.pack(fill="x", padx=16, pady=(0, 8))
        self.log_count_label = tk.Label(log_bar, text="", font=("Segoe UI", 8),
                                         fg=CLR["text2"], bg=CLR["bg"])
        self.log_count_label.pack(side="left")
        ttk.Button(log_bar, text="清空日志", style="Secondary.TButton",
                   command=self._clear_log).pack(side="right")

    def _make_stat_card(self, parent, label, value, col):
        card = tk.Frame(parent, bg=CLR["bg2"], highlightbackground=CLR["border"],
                        highlightthickness=1, padx=2, pady=2)
        card.pack(side="left", fill="x", expand=True, padx=(0 if col == 0 else 4, 4 if col == 3 else 0))

        tk.Label(card, text=label, font=("Segoe UI", 8), fg=CLR["text2"],
                 bg=CLR["bg2"]).pack(anchor="w", padx=8, pady=(8, 0))
        val_label = tk.Label(card, text=value, font=("Segoe UI", 12, "bold"),
                             fg=CLR["accent"], bg=CLR["bg2"])
        val_label.pack(anchor="w", padx=8, pady=(0, 8))

        # 存引用以便后续更新
        if not hasattr(self, "stat_labels"):
            self.stat_labels = {}
        self.stat_labels[col] = val_label

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 14, 14, fill=color, outline="")

    def _log(self, msg, tag=None):
        self.log_area.config(state="normal")
        self.log_area.insert("end", msg + "\n")
        self.log_area.see("end")
        self.log_area.config(state="disabled")
        self.log_lines.append(msg)
        # 保活
        if len(self.log_lines) > 500:
            self.log_lines = self.log_lines[-200:]
        self.log_count_label.config(text=f"{len(self.log_lines)} 条")

    def _clear_log(self):
        self.log_area.config(state="normal")
        self.log_area.delete("1.0", "end")
        self.log_area.config(state="disabled")
        self.log_lines.clear()
        self.log_count_label.config(text="0 条")

    # ── 启停逻辑 ──────────────────────────────────────
    def _start(self):
        if find_proxy_pid():
            self._log("[面板] 代理已在运行，跳过启动")
            self._update_ui_running()
            return

        self._log("[面板] 正在启动代理…")
        try:
            self.proxy_process = subprocess.Popen(
                [sys.executable, PROXY_SCRIPT],
                cwd=os.path.dirname(PROXY_SCRIPT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            time.sleep(2)
            if find_proxy_pid():
                self._log("[面板] ✅ 代理启动成功")
                self._update_ui_running()
            else:
                self._log("[面板] ⚠ 进程已创建但端口未监听，检查是否有错误")
                self._update_ui_stopped()
        except Exception as e:
            self._log(f"[面板] ❌ 启动失败: {e}")
            self._update_ui_stopped()

    def _stop(self):
        pid = find_proxy_pid()
        if not pid:
            self._log("[面板] 代理未运行")
            self._update_ui_stopped()
            return

        self._log(f"[面板] 正在停止代理 (PID {pid})…")
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10
            )
            time.sleep(1)
            if not find_proxy_pid():
                self._log("[面板] ✅ 代理已停止")
                self._update_ui_stopped()
            else:
                self._log("[面板] ⚠ 停止命令已发送但进程仍在")
        except Exception as e:
            self._log(f"[面板] ❌ 停止失败: {e}")

    def _restart(self):
        self._log("[面板] 正在重启代理…")
        self._stop()
        time.sleep(1)
        self._start()

    def _update_ui_running(self):
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_restart.config(state="normal")
        self._draw_dot(CLR["green"])
        self.status_label.config(text="运行中", fg=CLR["green"])

    def _update_ui_stopped(self):
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.btn_restart.config(state="disabled")
        self._draw_dot(CLR["red"])
        self.status_label.config(text="已停止", fg=CLR["red"])

    # ── 监控循环 ──────────────────────────────────────
    def _start_monitor(self):
        self.monitoring = True

        def monitor_loop():
            last_status = None
            while self.monitoring:
                try:
                    pid = find_proxy_pid()
                    health = http_get(HEALTH_URL)
                    stats = http_get(STATS_URL)

                    if pid and health:
                        # 运行中
                        if last_status != "running":
                            self.root.after(0, self._update_ui_running)
                            last_status = "running"

                        # 更新运行时长
                        if stats and "uptime" in stats:
                            uptime = stats["uptime"]
                            h, m = divmod(uptime, 3600)
                            m, s = divmod(m, 60)
                            uptime_str = f"{h}h{m}m" if h else f"{m}m{s}s"
                            self.root.after(0, lambda u=uptime_str: self.stat_labels[3].config(text=u))

                        # 更新统计卡片
                        if stats:
                            reqs = stats.get("total_requests", 0)
                            self.root.after(0, lambda r=reqs: self.stat_labels[0].config(text=str(r)))
                    else:
                        if last_status != "stopped":
                            self.root.after(0, self._update_ui_stopped)
                            last_status = "stopped"
                            self.stat_labels[3].config(text="--")

                except Exception:
                    pass

                time.sleep(POLL_INTERVAL / 1000)

        t = threading.Thread(target=monitor_loop, daemon=True)
        t.start()

    def _open_config(self):
        config_path = r"D:\claudian-bridge\proxy.py"
        try:
            os.startfile(config_path)
        except Exception:
            os.startfile(os.path.dirname(config_path))

    def _show_stats(self):
        stats = http_get(STATS_URL)
        if stats:
            total = stats.get("total_requests", 0)
            ok = stats.get("success_count", 0)
            fail = stats.get("error_count", 0)
            uptime_s = stats.get("uptime", 0)
            h, m = divmod(uptime_s, 3600)
            m, s = divmod(m, 60)
            uptime_str = f"{h}h{m}m{s}s" if h else f"{m}m{s}s"

            # 从 recent 计算平均耗时
            recent = stats.get("recent", [])
            if recent:
                avg_ms = round(sum(r["duration_ms"] for r in recent) / len(recent))
            else:
                avg_ms = 0

            lines = [
                f"总请求数:       {total}",
                f"成功:           {ok}",
                f"失败:           {fail}",
                f"转换量:         {stats.get('conversion_count', 0)}",
                f"运行时长:       {uptime_str}",
                f"成功率:         {round(ok / total * 100, 1) if total else 0}%",
                f"平均耗时(ms):   {avg_ms}",
            ]
            messagebox.showinfo("代理统计", "\n".join(lines))
        else:
            messagebox.showwarning("代理统计", "无法获取统计信息，代理可能未运行。")

    def _on_close(self):
        self.monitoring = False
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Panel().run()
