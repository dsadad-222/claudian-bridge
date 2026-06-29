#!/usr/bin/env python3
"""
Claudian Bridge — 轻量模型名替换代理

让 Claudian（Obsidian Claude 插件）通过 Claude CLI 接入任意第三方 Anthropic 兼容 API。
核心：拦截请求体中所有 "claude-*" 模型名，替换为目标模型名后透传。

用法:
    python proxy.py [--port 15722] [--target http://127.0.0.1:15721] [--model step-3.7-flash]

依赖: pip install requests
"""

import re
import sys
import argparse
import http.server
import requests


class ModelProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTP 代理处理器：拦截请求体中的模型名并替换"""

    pattern: re.Pattern = None
    replacement: str = ""
    target_url: str = ""

    @classmethod
    def configure(cls, target_url: str, replace_model: str):
        """配置代理参数"""
        cls.target_url = target_url.rstrip("/")
        cls.replacement = replace_model
        cls.pattern = re.compile(r'"model"\s*:\s*"claude-[^"]*"')

    def _forward(self, method: str):
        try:
            path = self.path
            body_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(body_len) if body_len else b""

            # 正则替换所有 claude-* 模型名
            body_str = body.decode("utf-8", errors="replace")
            found = self.pattern.findall(body_str)
            if found:
                body_str = self.pattern.sub(
                    f'"model": "{self.replacement}"', body_str
                )
                print(f"  [OK] {set(found)} → {self.replacement}")
            body_bytes = body_str.encode("utf-8")

            # 复制请求头（跳过连接相关头）
            headers = {}
            for k, v in self.headers.items():
                if k.lower() not in ("host", "content-length", "connection"):
                    headers[k] = v

            # 流式转发
            resp = requests.request(
                method=method,
                url=f"{self.target_url}{path}",
                headers=headers,
                data=body_bytes,
                stream=True,
                timeout=300,
            )

            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in (
                    "transfer-encoding",
                    "content-encoding",
                    "connection",
                ):
                    self.send_header(k, v)
            self.end_headers()

            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()

        except Exception as e:
            print(f"  [ERR] {e}")
            try:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(f"Proxy error: {e}".encode())
            except Exception:
                pass

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")

    def do_OPTIONS(self):
        self._forward("OPTIONS")

    def log_message(self, *args):
        pass  # 静默日志


def main():
    parser = argparse.ArgumentParser(
        description="Claudian Bridge — 模型名替换代理"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=15722,
        help="代理监听端口（默认 15722）",
    )
    parser.add_argument(
        "--target",
        default="http://127.0.0.1:15721",
        help="上游代理地址（默认 cc-switch 的 15721）",
    )
    parser.add_argument(
        "--model",
        default="step-3.7-flash",
        help="目标模型名（默认 step-3.7-flash）",
    )
    args = parser.parse_args()

    ModelProxyHandler.configure(
        target_url=args.target,
        replace_model=args.model,
    )

    print(f"Claudian Bridge v1.0")
    print(f"  Listen : 127.0.0.1:{args.port}")
    print(f"  Target : {args.target}")
    print(f"  Model  : claude-* → {args.model}")
    print(f"  Ready. Press Ctrl+C to stop.\n")

    server = http.server.HTTPServer(
        ("127.0.0.1", args.port), ModelProxyHandler
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        print("\nBye.")


if __name__ == "__main__":
    main()
