#!/usr/bin/env python3
"""
Claudian Bridge v2.2 — 多线程模型名替换代理 + Anthropic→OpenAI 格式转换

用法:
    python proxy.py --port 15722 --target https://api.stepfun.com/v1 --model step-3.7-flash

依赖: pip install requests
"""

import re
import os
import sys
import json
import time
import copy
import argparse
import threading
import http.server
import requests
import opencc

# 繁体 → 简体转换器（在桥接出口统一处理，避免上游模型吐繁体）
_t2s = opencc.OpenCC("t2s")

# ── 调试日志 ─────────────────────────────────
DEBUG_LOG = "D:\\claudian-bridge\\proxy_debug.log"
_debug_lock = threading.Lock()

def _debug(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    print(line, end="")
    try:
        with _debug_lock:
            with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass

# ── 调试落盘开关 ─────────────────────────────
# 默认关闭：每请求写 6 个 debug_*.json 会在目录里堆出上万个文件（曾达 6208 个）。
# 需要排查时设环境变量 CLAUDIAN_DEBUG_DUMP=1 再重启本服务。
DEBUG_DUMP = os.environ.get("CLAUDIAN_DEBUG_DUMP", "") == "1"
DEBUG_DIR = "D:\\claudian-bridge"


def _dump(fname, text):
    """按需落盘调试文件；返回写入路径，未启用或失败返回 None。"""
    if not DEBUG_DUMP:
        return None
    try:
        p = os.path.join(DEBUG_DIR, fname)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p
    except Exception:
        return None


# ── 统计追踪（线程安全） ──────────────────────
_stats_lock = threading.Lock()
_stats = {
    "start_time": time.time(),
    "total_requests": 0,
    "success_count": 0,
    "error_count": 0,
    "conversion_count": 0,
    "models": {},
    "recent": [],
}
MAX_RECENT = 100


def _record_request(original_model, target_model, status_code, duration_ms, tokens=0, converted=False):
    now = time.strftime("%H:%M:%S")
    with _stats_lock:
        _stats["total_requests"] += 1
        if 200 <= status_code < 300:
            _stats["success_count"] += 1
        else:
            _stats["error_count"] += 1
        if converted:
            _stats["conversion_count"] += 1
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
            "converted": converted,
        }
        _stats["recent"].append(entry)
        if len(_stats["recent"]) > MAX_RECENT:
            _stats["recent"] = _stats["recent"][-MAX_RECENT:]
        try:
            with open(r"D:\claudian-bridge\proxy_stats.json", "w", encoding="utf-8") as f:
                s = dict(_stats)
                s["uptime"] = round(time.time() - _stats["start_time"])
                json.dump(s, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


# ══════════════════════════════════════════════
#  Anthropic ↔ OpenAI 格式转换
# ══════════════════════════════════════════════

def _join_url(base, path):
    """拼接上游 URL。

    target 形如 http://127.0.0.1:8787/v1，客户端 path 形如 /v1/messages/count_tokens，
    直接相加会得到 /v1/v1/... → 404。这里在 base 以 /v1 结尾时剥掉 path 的 /v1 前缀。
    """
    base = (base or "").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    if base.endswith("/v1") and (path == "/v1" or path.startswith("/v1/")):
        path = path[3:] or "/"
    return base + path


def is_anthropic_messages_path(path):
    """判断请求路径是否为 Anthropic /v1/messages（含查询参数）"""
    pure_path = path.split("?")[0]
    return "/v1/messages" in pure_path and "/v1/messages/" not in pure_path and pure_path.rstrip("/").endswith("/v1/messages")


def anthropic_body_to_openai(body_str):
    """将 Anthropic messages 请求体转换为 OpenAI chat/completions 格式"""
    data = json.loads(body_str)

    messages = []
    system_content = None

    # 处理 system 提示
    system = data.get("system")
    if system:
        if isinstance(system, list):
            parts = []
            for s in system:
                if isinstance(s, dict):
                    parts.append(s.get("text", ""))
            system_content = "\n".join(p for p in parts if p)
        elif isinstance(system, str):
            system_content = system

    # inject system prompt
    if system_content:
        messages.append({"role": "system", "content": system_content})
    # if no system at all, skip adding empty system message

    # 处理 messages（正确转换 tool_use / tool_result）
    for msg in data.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content") or ""

        if isinstance(content, list):
            text_parts = []
            tool_calls = []
            reasoning_parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "thinking":
                    reasoning_parts.append(block.get("thinking", ""))
                elif block_type == "image":
                    text_parts.append(f"[image: {block.get('source', {}).get('media_type', 'unknown')}]")
                elif block_type == "tool_use":
                    # 转换为 OpenAI tool_calls 格式
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {}))
                        }
                    })
                elif block_type == "tool_result":
                    # tool_result → 单独一条 tool 消息
                    tool_content = block.get("content", "")
                    if isinstance(tool_content, list):
                        tool_content = "\n".join(
                            b.get("text", "") if isinstance(b, dict) else str(b)
                            for b in tool_content
                        )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": tool_content
                    })
            # assistant 消息：有 text、tool_calls 或 thinking
            if text_parts or tool_calls or reasoning_parts:
                msg_body = {"role": role}
                if text_parts:
                    msg_body["content"] = "\n".join(text_parts)
                if tool_calls:
                    msg_body["tool_calls"] = tool_calls
                # DeepSeek 要求对话延续时带回 reasoning_content
                if reasoning_parts:
                    msg_body["reasoning_content"] = "\n".join(reasoning_parts)
                messages.append(msg_body)
        else:
            messages.append({"role": role, "content": str(content)})

    openai_body = {
        "model": "claude-placeholder",  # will be replaced by model replacement logic
        "messages": messages,
    }

    # 透传参数（强制非流式，避免 SSE 复杂性）
    openai_body["stream"] = False
    for key in ("max_tokens", "temperature", "top_p", "stop"):
        if key in data:
            openai_body[key] = data[key]

    # ── 透传 tools（Anthropic → OpenAI 格式）──
    anthropic_tools = data.get("tools", [])
    if anthropic_tools:
        openai_tools = []
        for t in anthropic_tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {})
                }
            })
        openai_body["tools"] = openai_tools
        openai_body["tool_choice"] = "auto"  # 强制 StepFun 使用 function calling 而非文本输出
    

    return json.dumps(openai_body, ensure_ascii=False)


# 昵称矫正：上游免费模型有两个系统性怪癖——
#   ① 把昵称里的"小"翻译成英文 small（small渊/small渄/small林…）
#   ② 把"渊"写错为 渄/湵（小渄、small渄…）
# opencc 只处理繁→简，管不了这两类，故用规则化替换兜底（比枚举表更全，覆盖任意组合）。
_SMALL_CJK_RE = re.compile(r"(?i)small\s*([一-鿿])")

def _fix_nickname(t):
    # ① small + 中文字 → 小 + 中文字（大小写不敏感，中间可有空格；覆盖 small渊/small林/small渄 等任意组合）
    t = _SMALL_CJK_RE.sub(r"小\1", t)
    # ② "小渊"的"渊"常见错写
    t = t.replace("小渄", "小渊").replace("小湵", "小渊")
    return t

def _t2s_text(text):
    """把字符串中的繁体中文转为简体，并矫正模型对昵称的错误写法；非字符串原样返回。"""
    if isinstance(text, str) and text:
        try:
            t = _t2s.convert(text)
        except Exception:
            t = text
        return _fix_nickname(t)
    return text


def _t2s_content_blocks(parts):
    """递归转换 content blocks / tool 参数里的中文字符串。"""
    if isinstance(parts, list):
        return [_t2s_content_blocks(p) for p in parts]
    if isinstance(parts, dict):
        out = {}
        for k, v in parts.items():
            if k in ("text", "thinking") and isinstance(v, str):
                out[k] = _t2s_text(v)
            elif k == "input":
                # tool_use 参数 JSON 字符串可能含中文
                out[k] = _t2s_content_blocks(v)
            else:
                out[k] = v
        return out
    if isinstance(parts, str):
        return _t2s_text(parts)
    return parts


def openai_response_to_anthropic(resp_data, original_model):
    """将 OpenAI chat/completions 响应转换为 Anthropic messages 格式，并统一繁→简。"""
    choice = resp_data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content = _t2s_text(msg.get("content") or "")
    reasoning = _t2s_text(msg.get("reasoning_content") or msg.get("reasoning") or "")
    usage = resp_data.get("usage", {})
    model = resp_data.get("model", original_model)

    # 剥离模型手写的 <tool_call>...</tool_call> 伪标签（非流式，完整文本一次正则）
    content = re.sub(r'<tool_call>.*?</tool_call>\s*', '', content, flags=re.DOTALL)

    # 如果有 reasoning，拼接到内容前（用 thinking 块）
    content_parts = []
    if reasoning:
        content_parts.append({
            "type": "thinking",
            "thinking": reasoning,
        })
    if content:
        content_parts.append({
            "type": "text",
            "text": content,
        })

    # ── 转换 tool_calls（OpenAI → Anthropic content block）──
    openai_tool_calls = msg.get("tool_calls") or []
    for tc in openai_tool_calls:
        fn = tc.get("function", {})
        args_str = fn.get("arguments", "{}")
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = {}
        content_parts.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{int(time.time()):x}"),
            "name": fn.get("name", ""),
            "input": args,
        })

    # 确保始终有 content（没有 text 也没有 tool_use 时补空 text）
    if not content_parts:
        content_parts = [{"type": "text", "text": ""}]
    elif not any(p.get("type") == "text" for p in content_parts):
        content_parts.append({"type": "text", "text": ""})

    stop_reason = choice.get("finish_reason", "stop")
    if stop_reason == "stop":
        stop_reason = "end_turn"
    elif stop_reason == "length":
        stop_reason = "max_tokens"
    elif stop_reason == "tool_calls":
        stop_reason = "tool_use"

    # 统一繁→简：遍历 content_parts 里的 text/thinking/tool input
    content_parts = _t2s_content_blocks(content_parts)

    anthropic_resp = {
        "id": resp_data.get("id", f"msg_{int(time.time()):x}"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_parts,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }

    return anthropic_resp




# ══════════════════════════════════════════════
#  HTTP 处理器
# ══════════════════════════════════════════════

class ModelProxyHandler(http.server.BaseHTTPRequestHandler):

    pattern: re.Pattern = None
    replacement: str = ""
    target_url: str = ""
    api_key: str = ""
    model_map: dict = {}
    upstream_timeout: float = 300.0

    _inflight_lock = threading.Lock()
    _inflight_keys = set()

    @classmethod
    def configure(cls, target_url, replace_model, api_key="", model_map=None):
        cls.target_url = target_url.rstrip("/")
        cls.replacement = replace_model
        cls.api_key = api_key
        cls.model_map = model_map or {}
        cls.pattern = re.compile(r'"model"\s*:\s*"[^"]*"')

    @classmethod
    def resolve_model(cls, incoming):
        """把客户端模型名解析成真实上游模型名。

        优先按 model_map 做「子串匹配」（键不分大小写），例如
        {"claude-opus": "deepseek-v4-pro", "claude-haiku": "deepseek-v4-flash"}
        命中即用映射值；未命中一律用 replacement（默认模型）。
        """
        name = (incoming or "").strip()
        low = name.lower()
        for key, val in (cls.model_map or {}).items():
            if key and key.lower() in low:
                return val
        return cls.replacement

    def _forward(self, method):
        _debug(f"=== _forward START method={method} path={self.path} ===")
        start_time = time.time()
        original_model = "unknown"
        status_code = 502
        total_bytes = 0
        was_converted = False
        try:
            path = self.path
            body_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(body_len) if body_len else b""
            _debug(f"Read body: {body_len} bytes")

            body_str = body.decode("utf-8", errors="replace")

            # 先抓客户端原始模型名 —— 转换器会把 model 硬写成 claude-placeholder，
            # 转换之后再取就拿不到真实名字，model_map 分流会全部落到默认档。
            _m0 = re.search(r'"model"\s*:\s*"([^"]*)"', body_str)
            requested_model = _m0.group(1) if _m0 else ""
            if requested_model:
                original_model = requested_model

            # ── 检测是否需要 Anthropic→OpenAI 转换 ──
            print(f"  [PATH] {method} {path}")
            do_convert = False
            if method == "POST" and is_anthropic_messages_path(path):
                do_convert = True
                was_converted = True
                original_path = path
                # DEBUG: 保存原始 Anthropic 请求体（默认关闭）
                _dump(f"debug_anth_orig_{int(time.time()*1000)}.json", body_str[:50000])
                try:
                    body_str = anthropic_body_to_openai(body_str)
                    path = "/chat/completions"
                    print(f"  [CONVERT] Anthropic → OpenAI ({len(body)} → {len(body_str)} bytes)")
                except Exception as e:
                    print(f"  [CONVERT_ERR] {e}")
                    self._send_json(400, {"error": "conversion_failed", "message": str(e)})
                    return

            # ── 模型名替换 ──
            found = self.pattern.findall(body_str)
            if found:
                if not requested_model:
                    for f in found:
                        m = re.search(r'"([^"]*)"$', f)
                        if m:
                            original_model = m.group(1)
                            break
                resolved = self.resolve_model(requested_model or original_model)
                body_str = self.pattern.sub(
                    f'"model": "{resolved}"', body_str
                )
                print(f"  [MODEL] {original_model} → {resolved}")

            body_bytes = body_str.encode("utf-8")

            # ── 调试：记录请求体（默认关闭，见 CLAUDIAN_DEBUG_DUMP）──
            _dump(f"debug_req_{int(time.time()*1000)}.json", body_str[:50000])

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
                forward_headers = {}
                for k, v in self.headers.items():
                    kl = k.lower()
                    if kl not in ("host", "content-length", "connection",
                                  "transfer-encoding"):
                        forward_headers[k] = v
                forward_headers["Connection"] = "close"

                if self.api_key:
                    forward_headers["Authorization"] = f"Bearer {self.api_key}"
                    forward_headers["x-api-key"] = self.api_key

                target_url = _join_url(self.target_url, path)
                _debug(f"Sending to StepFun: {method} {target_url} ({len(body_bytes)} bytes)")
                _debug(f"Headers: Auth={'present' if self.api_key else 'MISSING'}")
                resp = requests.request(
                    method=method,
                    url=target_url,
                    headers=forward_headers,
                    data=body_bytes,
                    stream=True,
                    timeout=(10, self.upstream_timeout),
                )
                status_code = resp.status_code
                _debug(f"StepFun response: {status_code}")

                # ── DeepSeek reasoning_content 兜底重试 ──
                if was_converted and status_code == 400:
                    try:
                        err_text = resp.text[:2000]
                        if "reasoning_content" in err_text and "must be passed back" in err_text:
                            print("  [RETRY] reasoning_content missing in history, stripping and retrying...")
                            body_obj = json.loads(body_str)
                            for msg in body_obj.get("messages", []):
                                if msg.get("role") == "assistant":
                                    msg.pop("reasoning_content", None)
                            body_str = json.dumps(body_obj, ensure_ascii=False)
                            body_bytes = body_str.encode("utf-8")
                            forward_headers["Content-Length"] = str(len(body_bytes))
                            resp = requests.request(
                                method=method,
                                url=target_url,
                                headers=forward_headers,
                                data=body_bytes,
                                stream=True,
                                timeout=(10, self.upstream_timeout),
                            )
                            status_code = resp.status_code
                            print(f"  [RETRY] Status: {status_code}")
                    except Exception:
                        pass

                # ── 逆向转换（OpenAI → Anthropic），始终非流式 ──
                if was_converted and status_code == 200:
                    try:
                        _debug("Starting OpenAI->Anthropic response conversion")
                        resp_body = b""
                        for chunk in resp.iter_content(chunk_size=65536):
                            resp_body += chunk
                        _debug(f"Response body collected: {len(resp_body)} bytes")
                        # DEBUG: 保存 OpenAI 原始响应（默认关闭）
                        _dump(f"debug_oai_resp_{int(time.time()*1000)}.json",
                              resp_body.decode("utf-8", errors="replace"))
                        openai_data = json.loads(resp_body.decode("utf-8"))
                        _debug(f"Parsed OpenAI JSON, keys: {list(openai_data.keys())}")
                        anthropic_data = openai_response_to_anthropic(openai_data, self.replacement)
                        _debug(f"Converted to Anthropic, keys: {list(anthropic_data.keys())}")
                        response_bytes = json.dumps(anthropic_data, ensure_ascii=False).encode("utf-8")
                        print(f"  [CONVERT] OpenAI → Anthropic ({len(resp_body)} → {len(response_bytes)} bytes)")
                        # DEBUG: 保存 Anthropic 响应（默认关闭）
                        _dump(f"debug_anth_resp_{int(time.time()*1000)}.json",
                              json.dumps(anthropic_data, ensure_ascii=False, indent=2))

                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(response_bytes)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(response_bytes)
                        total_bytes = len(response_bytes)
                        _debug("Conversion SUCCESS, response sent")
                    except Exception as e:
                        _debug(f"RCONVERT_ERR: {e}")
                        # 保存原始响应用于调试
                        raw_path = _dump(f"debug_raw_resp_{int(time.time()*1000)}.txt",
                                         resp_body.decode("utf-8", errors="replace")[:2000])
                        if raw_path:
                            _debug(f"Raw response saved to {raw_path}")
                        import traceback; _debug(traceback.format_exc())
                        print(f"  [RCONVERT_ERR] {e}")
                        self._send_json(502, {"error": "response_conversion_failed", "message": str(e)})
                        status_code = 502
                elif was_converted and status_code != 200:
                    # 转换模式下的错误，直接返回上游错误
                    error_body = resp.text.encode("utf-8")
                    # ── 调试：记录上游错误响应 ──
                    err_path = _dump(f"debug_err_{int(time.time()*1000)}.json", resp.text[:10000])
                    if err_path:
                        print(f"  [UPSTREAM_ERR] {status_code} → {err_path}")
                    else:
                        print(f"  [UPSTREAM_ERR] {status_code}: {resp.text[:500]}")
                    self.send_response(status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(error_body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(error_body)
                    total_bytes = len(error_body)
                else:
                    # 普通透传模式
                    self.send_response(status_code)
                    for k, v in resp.headers.items():
                        kl = k.lower()
                        if kl not in ("transfer-encoding", "content-encoding",
                                      "connection", "keep-alive"):
                            self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()

                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            try:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                                total_bytes += len(chunk)
                            except (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError, OSError):
                                print(f"  [INFO] 客户端断开 (已传输 {total_bytes} 字节)")
                                break

                    if not was_converted and status_code >= 400:
                        print(f"  [UPSTREAM_ERR] {status_code} on {method} {path}")

            finally:
                with self._inflight_lock:
                    self._inflight_keys.discard(inflight_key)

        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            _debug("EXCEPTION: CLIENT_GONE")
            print(f"  [CLIENT_GONE]")
            status_code = 499

        except requests.exceptions.Timeout as e:
            _debug(f"EXCEPTION: TIMEOUT {e}")
            print(f"  [TIMEOUT] {e}")
            status_code = 504
            try:
                self._send_json(504, {"error": "upstream_timeout", "retryable": True})
            except Exception:
                pass

        except requests.exceptions.ConnectionError as e:
            _debug(f"EXCEPTION: CONN_ERR {e}")
            print(f"  [CONN_ERR] {e}")
            status_code = 502
            try:
                self._send_json(502, {"error": "upstream_unavailable", "retryable": True})
            except Exception:
                pass

        except Exception as e:
            _debug(f"EXCEPTION: FATAL {type(e).__name__}: {e}")
            print(f"  [FATAL] {type(e).__name__}: {e}")
            status_code = 502

        duration = (time.time() - start_time) * 1000
        _debug(f"=== _forward END status={status_code} duration={duration:.0f}ms ===")
        _record_request(original_model, self.replacement, status_code, duration, 0,
                        converted=was_converted)

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
            # Return common Claude model names so Claudian doesn't complain
            known_models = [
                "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
                "claude-3-opus-20240229", "claude-3-sonnet-20240229",
                "claude-3-haiku-20240307", "claude-haiku-4-5-20251001",
                "claude-sonnet-4-20250514", "claude-opus-4-20250514",
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


def _load_bridge_config(base_dir):
    """读取同目录下的 bridge_config.json。

    返回 (cfg, path)。文件不存在返回 (None, path)；解析失败也返回 (None, path)。
    """
    cfg_path = os.path.join(base_dir, "bridge_config.json")
    if not os.path.exists(cfg_path):
        return None, cfg_path
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("root is not an object")
        return cfg, cfg_path
    except Exception as e:
        print(f"  [CFG_ERR] {cfg_path} 解析失败: {e}")
        return None, cfg_path


def main():
    parser = argparse.ArgumentParser(
        description="Claudian Bridge v2.3 — 模型名替换 + Anthropic→OpenAI 转换"
    )
    parser.add_argument("--port", type=int, default=15722)
    parser.add_argument("--target", default="https://api.stepfun.com/v1")
    parser.add_argument("--model", default="deepseek-v4-flash-free")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    # ── 上游配置：bridge_config.json 为准，缺失时回落到内置默认 ──
    # 命令行 --target/--model/--api-key 会被配置文件覆盖（避免 guardian 里的旧参数打架），
    # 想换上游/模型请编辑 bridge_config.json 后重启本服务。
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg, cfg_path = _load_bridge_config(base_dir)
    if cfg:
        args.target = str(cfg.get("target") or args.target)
        args.model = str(cfg.get("model") or args.model)
        args.api_key = str(cfg.get("api_key") or "")
        model_map = {str(k): str(v) for k, v in (cfg.get("model_map") or {}).items() if v}
        cfg_note = f"loaded {cfg_path}"
    else:
        args.target = "http://127.0.0.1:18790/v1"
        args.model = "deepseek-v4.1-flash"
        args.api_key = "sk-free"
        model_map = {}
        cfg_note = f"{cfg_path} missing -> built-in default"

    ModelProxyHandler.configure(
        target_url=args.target,
        replace_model=args.model,
        api_key=args.api_key,
        model_map=model_map,
    )
    ModelProxyHandler.upstream_timeout = args.timeout

    print(f"Claudian Bridge v2.3 (Anthropic→OpenAI)")
    print(f"  Listen  : 127.0.0.1:{args.port}")
    print(f"  Config  : {cfg_note}")
    print(f"  Target  : {args.target}")
    print(f"  Model   : * → {args.model}")
    for k, v in model_map.items():
        print(f"            {k}* → {v}")
    print(f"  Stats   : http://127.0.0.1:{args.port}/stats")
    print(f"  Ready.\n")

    server = ThreadedHTTPServer(("127.0.0.1", args.port), ModelProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        print("\nBye.")


if __name__ == "__main__":
    main()
