#!/usr/bin/env python3
"""
Claudian Bridge v2.1 — 多线程模型名替换代理 (OpenAI 兼容端点)

让 VS Code Continue 通过 Claude Bridge 接入 StepFun OpenAI 兼容 API。
核心：替换请求体中所有 "claude-*" 模型名为目标模型名后透传，并注入 Authorization header。

用法:
    python proxy_openai.py --port 15723 --target https://api.stepfun.com/v1 --model step-3.7-flash

依赖: pip install requests
"""

import re
import sys
import json
import time
import argparse
import threading
import http.server
import requests

# ── 统计追踪（线程安全） ──────────────────────
_stats_lock = threading.Lock()
_stats = {
    "start_time": time.time(),
    "total_requests": 0,
    "success_count": 0,
    "error_count": 0,
    "models": {},
    "recent": [],
}
MAX_RECENT = 100


def _record_request(original_model, target_model, status_code, duration_ms, tokens=0):
    now = time.strftime("%H:%M:%S")
    with _stats_lock:
        _stats["total_requests"] += 1
        if 200 <= status_code < 300:
            _stats["success_count"] += 1
        else:
            _stats["error_count"] += 1
        if original_model:
            key = f"{original_model}→{target_model}"
            if key not in _stats["models"]:
                _stats["models"][key] = {"requests": 0, "total_tokens": 0}
            _stats["models"][key]["requests"] += 1
            _stats["models"][key]["total_tokens"] += tokens
        entry = {
            "time": now, "original_model": original_model,
            "target_model": target_model, "status": status_code,
            "total_tokens": tokens, "duration_ms": round(duration_ms, 1),
        }
        _stats["recent"].append(entry)
        if len(_stats["recent"]) > MAX_RECENT:
            _stats["recent"] = _stats["recent"][-MAX_RECENT:]
        try:
            with open(r"D:\claudian-bridge\proxy_openai_stats.json", "w", encoding="utf-8") as f:
                s = dict(_stats)
                s["uptime"] = round(time.time() - _stats["start_time"])
                json.dump(s, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


class ModelProxyHandler(http.server.BaseHTTPRequestHandler):
    """多线程安全的 HTTP 代理处理器"""

    pattern: re.Pattern = None
    replacement: str = ""
    target_url: str = ""
    upstream_timeout: float = 300.0
    auth_header: str = ""

    # 请求去重
    _inflight_lock = threading.Lock()
    _inflight_keys = set()

    @classmethod
    def configure(cls, target_url: str, replace_model: str, auth_header: str = ""):
        cls.target_url = target_url.rstrip("/")
        cls.replacement = replace_model
        cls.auth_header = auth_header
        # 匹配所有 "model": "xxx" 的 JSON 字段
        cls.pattern = re.compile(r'"model"\s*:\s*"[^"]*"')

    def _forward(self, method: str):
        import datetime
        with open(r"D:\claudian-bridge\proxy_v2.log", "a", encoding="utf-8") as lf:
            lf.write(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {method} {self.path}\n")
        start_time = time.time()
        original_model = "unknown"
        status_code = 502
        total_bytes = 0
        try:
            path = self.path
            body_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(body_len) if body_len else b""

            # ── 模型名替换 ──
            body_str = body.decode("utf-8", errors="replace")
            found = self.pattern.findall(body_str)
            if found:
                for f in found:
                    m = re.search(r'"([^"]*)"$', f)
                    if m:
                        original_model = m.group(1)
                        break
                body_str = self.pattern.sub(
                    f'"model": "{self.replacement}"', body_str
                )
                print(f"  [MODEL] {original_model} → {self.replacement}")

            # 判断是否为流式请求（检查请求体中的 stream 字段）
            is_stream_request = False
            try:
                req_json = json.loads(body_str)
                is_stream_request = req_json.get("stream", False)
            except Exception:
                pass
            body_bytes = body_str.encode("utf-8")

            # ── 请求去重 ──
            body_hash = hash(body_bytes)
            inflight_key = (method, path, body_hash)
            with self._inflight_lock:
                if inflight_key in self._inflight_keys:
                    print(f"  [DEDUP] 跳过重复请求 {method} {path}")
                    self._send_json(409, {
                        "error": "duplicate request",
                        "message": "An identical request is already in flight."
                    })
                    return
                self._inflight_keys.add(inflight_key)

            try:
                # 构建转发请求头（透传所有，跳过连接相关头，注入 Authorization）
                forward_headers = {}
                for k, v in self.headers.items():
                    kl = k.lower()
                    if kl not in ("host", "content-length", "connection",
                                  "transfer-encoding", "authorization"):
                        forward_headers[k] = v
                forward_headers["Connection"] = "close"
                # 注入 Authorization header（覆盖客户端发来的任何 Authorization）
                if self.auth_header:
                    forward_headers["Authorization"] = self.auth_header

                # 转发到上游
                resp = requests.request(
                    method=method,
                    url=f"{self.target_url}{path}",
                    headers=forward_headers,
                    data=body_bytes,
                    stream=True,
                    timeout=(10, self.upstream_timeout),
                )
                status_code = resp.status_code

                self.send_response(status_code)
                for k, v in resp.headers.items():
                    kl = k.lower()
                    if kl not in ("transfer-encoding", "content-encoding",
                                  "content-length", "connection", "keep-alive"):
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()

                if is_stream_request:
                    # 流式响应：逐行解析 SSE，合并 reasoning_content → content
                    with open(r"D:\claudian-bridge\proxy_v2.log", "a", encoding="utf-8") as lf:
                        lf.write(f"  [STREAM] start\n")
                    line_buffer = b""
                    chunk_count = 0
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        try:
                            line_buffer += chunk
                            # 按 \n 分割，处理完整行
                            while b"\n" in line_buffer:
                                line, line_buffer = line_buffer.split(b"\n", 1)
                                line = line.rstrip(b"\r")
                                raw_line = line
                                if not line:
                                    # 空行（SSE 分隔符）
                                    self.wfile.write(b"\n")
                                    self.wfile.flush()
                                    total_bytes += 1
                                    continue
                                if line.startswith(b"data: "):
                                    data_str = line[6:]  # 去掉 "data: "
                                    if data_str == b"[DONE]":
                                        self.wfile.write(b"data: [DONE]\n")
                                        self.wfile.flush()
                                        total_bytes += len(b"data: [DONE]\n")
                                    else:
                                        try:
                                            obj = json.loads(data_str)
                                            choices = obj.get("choices", [])
                                            if choices:
                                                delta = choices[0].get("delta", {})
                                                content = delta.get("content")
                                                reasoning = delta.get("reasoning_content")
                                                if (content is None or content == "") and reasoning:
                                                    delta["content"] = reasoning
                                                    print("  [MERGE_STREAM] delta reasoning_content → content")
                                            chunk_count += 1
                                            if chunk_count <= 3:
                                                with open(r"D:\claudian-bridge\proxy_v2.log", "a", encoding="utf-8") as lf:
                                                    lf.write(f"  [CHUNK{chunk_count}] content='{str(content)[:50]}' reasoning='{str(reasoning)[:50]}'\n")
                                            modified = json.dumps(obj, ensure_ascii=False)
                                            out_line = f"data: {modified}\n".encode("utf-8")
                                            self.wfile.write(out_line)
                                            self.wfile.flush()
                                            total_bytes += len(out_line)
                                        except json.JSONDecodeError:
                                            # 无法解析，原样输出
                                            out_line = b"data: " + data_str + b"\n"
                                            self.wfile.write(out_line)
                                            self.wfile.flush()
                                            total_bytes += len(out_line)
                                else:
                                    # 非 data: 开头的行，原样输出
                                    self.wfile.write(raw_line + b"\n")
                                    self.wfile.flush()
                                    total_bytes += len(raw_line) + 1
                        except (BrokenPipeError, ConnectionResetError,
                                ConnectionAbortedError, OSError):
                            print(f"  [INFO] 客户端断开 (已传输 {total_bytes} 字节)")
                            break
                    with open(r"D:\claudian-bridge\proxy_v2.log", "a", encoding="utf-8") as lf:
                        lf.write(f"  [STREAM] end, total_bytes={total_bytes}, chunks={chunk_count}\n")
                else:
                    # 非流式响应：收集完整 body，合并 reasoning_content → content
                    with open(r"D:\claudian-bridge\proxy_v2.log", "a", encoding="utf-8") as lf:
                        lf.write(f"  [NONSTREAM] start\n")
                    raw_body = b""
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            raw_body += chunk

                    try:
                        resp_json = json.loads(raw_body.decode("utf-8"))
                        choices = resp_json.get("choices", [])
                        if choices:
                            msg = choices[0].get("message", {})
                            content = msg.get("content")
                            reasoning = msg.get("reasoning_content")
                            if (content is None or content == "") and reasoning:
                                msg["content"] = reasoning
                                print(f"  [MERGE] reasoning_content → content ({len(reasoning)} chars)")
                                raw_body = json.dumps(resp_json, ensure_ascii=False).encode("utf-8")
                    except Exception as e:
                        print(f"  [MERGE_ERR] {e}")

                    try:
                        self.wfile.write(raw_body)
                        self.wfile.flush()
                        total_bytes = len(raw_body)
                    except (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError, OSError):
                        print(f"  [INFO] 客户端断开 (已传输 {total_bytes} 字节)")

                if status_code >= 400:
                    print(f"  [UPSTREAM_ERR] {status_code} on {method} {path}")

            finally:
                with self._inflight_lock:
                    self._inflight_keys.discard(inflight_key)

        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            print(f"  [CLIENT_GONE]")
            status_code = 499

        except requests.exceptions.Timeout as e:
            print(f"  [TIMEOUT] {e}")
            status_code = 504
            try:
                self._send_json(504, {"error": "upstream_timeout", "retryable": True})
            except Exception:
                pass

        except requests.exceptions.ConnectionError as e:
            print(f"  [CONN_ERR] {e}")
            status_code = 502
            try:
                self._send_json(502, {"error": "upstream_unavailable", "retryable": True})
            except Exception:
                pass

        except Exception as e:
            print(f"  [FATAL] {type(e).__name__}: {e}")
            status_code = 502

        duration = (time.time() - start_time) * 1000
        _record_request(original_model, self.replacement, status_code, duration, 0)

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        if self.path == "/stats":
            with _stats_lock:
                s = dict(_stats)
                s["uptime"] = round(time.time() - _stats["start_time"])
            self._send_json(200, s)
            return
        if self.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "target": self.target_url,
                "model": self.replacement,
            })
            return
        if self.path in ("/v1/models", "/models"):
            known_models = [
                self.replacement,
                "claude-3-5-sonnet-20241022",
                "claude-3-5-haiku-20241022",
                "claude-3-opus-20240229",
                "claude-3-sonnet-20240229",
                "claude-3-haiku-20240307",
                "claude-haiku-4-5-20251001",
                "claude-sonnet-4-20250514",
                "claude-opus-4-20250514",
                "claude-opus-4-1-20250805",
                "claude-sonnet-4-5-20250929",
            ]
            self._send_json(200, {
                "object": "list",
                "data": [{"id": m, "object": "model"} for m in known_models],
            })
            return
        self._forward("GET")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                        "Content-Type, Authorization, x-api-key, "
                        "anthropic-version, x-stainless-*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_DELETE(self):
        self._forward("DELETE")

    def _send_json(self, code, data):
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, *args):
        pass


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        print(f"  [SERVER_ERR] {client_address}")


def main():
    parser = argparse.ArgumentParser(
        description="Claudian Bridge v2.1 — 模型名替换代理 (OpenAI)"
    )
    parser.add_argument("--port", type=int, default=15723)
    parser.add_argument("--target", default="https://api.stepfun.com")
    parser.add_argument("--model", default="step-3.7-flash")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--auth", default="Bearer 5vCB3dDdVzPK8RyN5KTDG68edDN1cs05t400BlykYm3e1h5TmNXwAiShrwdzVWe0o")
    args = parser.parse_args()

    ModelProxyHandler.configure(
        target_url=args.target,
        replace_model=args.model,
        auth_header=args.auth,
    )
    ModelProxyHandler.upstream_timeout = args.timeout

    # 重定向 stdout/stderr 到日志文件
    log_file = open(r"D:\claudian-bridge\proxy_openai.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file

    print(f"Claudian Bridge v2.1 (OpenAI)")
    print(f"  Listen  : 127.0.0.1:{args.port}")
    print(f"  Target  : {args.target}")
    print(f"  Model   : * → {args.model}")
    print(f"  Stats   : http://127.0.0.1:{args.port}/stats")
    print(f"  Ready.\n")

    server = ThreadedHTTPServer(("127.0.0.1", args.port), ModelProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        print("\nBye.")
        log_file.close()


if __name__ == "__main__":
    main()
