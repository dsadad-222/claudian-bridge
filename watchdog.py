#!/usr/bin/env python3
"""
双桥守护者 — 看管 Zen Bridge (18790) 和 Claudian Bridge (15722)
用法: pythonw watchdog.py
"""
import subprocess
import time
import sys
import os

# ====== 配置 ======
ZEN_SCRIPT   = r"D:\op-zen-bridge\proxy.py"
ZEN_PORT     = 18790
CL_SCRIPT    = r"D:\claudian-bridge\proxy.py"
CL_PORT      = 15722
CHECK_INTERVAL = 5

# ====== 工具函数 ======
def port_owner(port):
    """返回监听 port 的进程 PID，无则返回 None"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue).OwningProcess"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        pid = r.stdout.strip()
        return int(pid) if pid and pid.isdigit() else None
    except:
        return None

def start_proxy(script, cwd):
    return subprocess.Popen(
        [sys.executable, script],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

# ====== 主循环 ======
if __name__ == "__main__":
    print("[watchdog] Dual-bridge watchdog started")
    while True:
        # 1. 先保 Zen Bridge（Claudian Bridge 依赖它）
        zen_pid = port_owner(ZEN_PORT)
        if not zen_pid:
            print(f"[watchdog] Zen Bridge ({ZEN_PORT}) dead, restarting...")
            start_proxy(ZEN_SCRIPT, os.path.dirname(ZEN_SCRIPT))
            time.sleep(3)
            zen_pid = port_owner(ZEN_PORT)
            if zen_pid:
                print(f"[watchdog] Zen Bridge OK (PID={zen_pid})")
            else:
                print("[watchdog] Zen Bridge startup FAILED")

        # 2. 再保 Claudian Bridge
        cl_pid = port_owner(CL_PORT)
        if not cl_pid:
            print(f"[watchdog] Claudian Bridge ({CL_PORT}) dead, restarting...")
            start_proxy(CL_SCRIPT, os.path.dirname(CL_SCRIPT))
            time.sleep(3)
            cl_pid = port_owner(CL_PORT)
            if cl_pid:
                print(f"[watchdog] Claudian Bridge OK (PID={cl_pid})")
            else:
                print("[watchdog] Claudian Bridge startup FAILED")

        time.sleep(CHECK_INTERVAL)
