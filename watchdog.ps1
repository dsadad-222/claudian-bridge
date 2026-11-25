# 双桥守护者 v3 — 带日志的重启保护
# 看管 Zen Bridge (:18790) 和 Obsidian Gateway (:5722 -> 上游见 bridge_config.json)
# v3.1: 2026-08-22 改用受控 venv pythonw — Marvis runtime 的 certifi 证书包损坏，导致 HTTPS 上游 500

$pyw     = "C:\Users\y7000\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"
$zenPort = 18790
$zenPy   = "D:\op-zen-bridge\proxy.py"
$clPort  = 5722
$clPy    = "D:\claudian-bridge\proxy.py"
$clArgs  = "--port 5722 --timeout 600"   # upstream target/model/apikey now come from D:\claudian-bridge\bridge_config.json
$logFile = "D:\claudian-bridge\watchdog.log"

# 锁文件：防止多实例
$lockFile = "$env:TEMP\bridge_watchdog.lock"
if (Test-Path $lockFile) {
    $oldPid = Get-Content $lockFile
    $oldProc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($oldProc) { exit }
}
$PID | Out-File $lockFile -Force

function Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File $logFile -Append -Encoding utf8
}

Log "=== Watchdog v2 started ==="
Log "Zen port=$zenPort, Claudian port=$clPort"

$zenRestarts = 0
$clRestarts = 0

while ($true) {
    # 1. Zen Bridge — 必须确保 18790 只跑 Python (Zen Bridge)
    $occupant = Get-NetTCPConnection -LocalPort $zenPort -State Listen -ErrorAction SilentlyContinue
    if (-not $occupant) {
        $zenRestarts++
        Log "Zen DEAD (restart #$zenRestarts)"
        Start-Process $pyw -ArgumentList $zenPy -WindowStyle Hidden
        Start-Sleep 3
        $zen2 = Get-NetTCPConnection -LocalPort $zenPort -State Listen -ErrorAction SilentlyContinue
        if ($zen2) { Log "Zen RESTARTED" } else { Log "Zen FAILED to restart" }
    } else {
        # 端口被占，验证是不是 Zen Bridge 本尊
        $owner = Get-Process -Id $occupant.OwningProcess -ErrorAction SilentlyContinue
        if ($owner.ProcessName -ne "pythonw" -and $owner.ProcessName -ne "python") {
            $zenRestarts++
            Log "Zen PORT STOLEN by $($owner.ProcessName) (PID $($owner.Id)), killing and restarting..."
            Stop-Process -Id $owner.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep 1
            Start-Process $pyw -ArgumentList $zenPy -WindowStyle Hidden
            Start-Sleep 3
            $zen2 = Get-NetTCPConnection -LocalPort $zenPort -State Listen -ErrorAction SilentlyContinue
            if ($zen2) { Log "Zen RECLAIMED from $($owner.ProcessName)" } else { Log "Zen FAILED to reclaim" }
        }
    }

    # 2. Obsidian Gateway (:5722 -> 18790)
    $cl = Get-NetTCPConnection -LocalPort $clPort -State Listen -ErrorAction SilentlyContinue
    if (-not $cl) {
        $clRestarts++
        Log "Obsidian Gateway DEAD (restart #$clRestarts)"
        Start-Process $pyw -ArgumentList @($clPy, $clArgs) -WindowStyle Hidden
        Start-Sleep 3
        $cl2 = Get-NetTCPConnection -LocalPort $clPort -State Listen -ErrorAction SilentlyContinue
        if ($cl2) { Log "Obsidian Gateway RESTARTED" } else { Log "Obsidian Gateway FAILED to restart" }
    }

    # 每 10 分钟报一次心跳
    $now = Get-Date
    if ($now.Minute % 10 -eq 0 -and $now.Second -lt 10) {
        $zenOk = if (Get-NetTCPConnection -LocalPort $zenPort -State Listen -ErrorAction SilentlyContinue) { "OK" } else { "DEAD" }
        $clOk  = if (Get-NetTCPConnection -LocalPort $clPort -State Listen -ErrorAction SilentlyContinue) { "OK" } else { "DEAD" }
        Log "Heartbeat: Zen=$zenOk Claudian=$clOk (restarts: Z=$zenRestarts C=$clRestarts)"
    }

    Start-Sleep -Seconds 5
}
