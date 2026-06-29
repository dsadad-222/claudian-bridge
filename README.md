---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: bc0971d52bec790f559be11d27ddfafe_36afab8273e111f1986d525400d9a7a1
    ReservedCode1: B84KSHBPVtINMNu649Q9Q8pcVfQr5+BaXU/+uRMSiOUulFC4oA5dJo9RTUZagyPZSflS89pZCp6b9dIG2uZqesHnwAyY2URRdUWPphluG7PR2He8u7UFhnILg1VYVp1vc1olD5jzi1f4ZF2oDhI+ER+RNspEsUmFnXkzy4sooslsrV3HOUSjiXbE3hw=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: bc0971d52bec790f559be11d27ddfafe_36afab8273e111f1986d525400d9a7a1
    ReservedCode2: B84KSHBPVtINMNu649Q9Q8pcVfQr5+BaXU/+uRMSiOUulFC4oA5dJo9RTUZagyPZSflS89pZCp6b9dIG2uZqesHnwAyY2URRdUWPphluG7PR2He8u7UFhnILg1VYVp1vc1olD5jzi1f4ZF2oDhI+ER+RNspEsUmFnXkzy4sooslsrV3HOUSjiXbE3hw=
---



# Claudian Bridge

**60 行 Python，让 Obsidian 里的 Claudian 接入任意第三方 Anthropic 兼容 API。**

Claudian 调用 Claude CLI 时强制使用 Anthropic 原生模型名（如 `claude-sonnet-4-20250514`），第三方 API 平台（StepFun、SiliconFlow、DeepSeek 等）不认识这些名字，直接返回 404。cc-switch 的模型路由只对 Claude Desktop 生效，Claude Code 的 `modelOverrides` 又被 `--model` 参数覆盖。

本代理在 HTTP 层用正则拦截并替换模型名，无需修改任何一方配置。

## 架构

```
Claudian (Obsidian)
  │
  ▼
Claude CLI ── model: claude-sonnet-4-20250514
  │
  ▼
Claudian Bridge :15722 ── 正则替换为 step-3.7-flash
  │
  ▼
cc-switch :15721 ── 透传 Anthropic Messages 格式
  │
  ▼
StepFun / SiliconFlow / DeepSeek ... ✅
```

## 快速开始

### 1. 安装依赖

```bash
pip install requests
```

### 2. 启动代理

```bash
# 默认配置：监听 15722 → cc-switch 15721，模型名 → step-3.7-flash
python proxy.py

# 自定义目标模型
python proxy.py --model deepseek-chat

# 自定义端口和上游
python proxy.py --port 9999 --target https://api.stepfun.com/step_plan --model step-3.7-flash
```

### 3. 修改 Claude CLI 配置

编辑 `~/.claude/settings.json`，将 `ANTHROPIC_BASE_URL` 指向代理：

```diff
{
  "env": {
-   "ANTHROPIC_BASE_URL": "http://127.0.0.1:15721",
+   "ANTHROPIC_BASE_URL": "http://127.0.0.1:15722",
    "ANTHROPIC_AUTH_TOKEN": "sk-placeholder"
  }
}
```

### 4. 配置 cc-switch（或直接指向第三方 API）

**方案 A：通过 cc-switch**（推荐，统一管理多平台）

在 cc-switch 中添加目标平台的 Provider，Claudian Bridge 只做模型名替换，cc-switch 负责鉴权和转发。

**方案 B：直接指向第三方 API**

跳过 cc-switch，直接连接：

```bash
python proxy.py --target https://api.stepfun.com/step_plan --model step-3.7-flash
```

同时在 `~/.claude/settings.json` 中设置真实的 API Key：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:15722",
    "ANTHROPIC_AUTH_TOKEN": "你的API Key"
  }
}
```

## 工作原理

Core logic in 3 lines:

```python
pattern = re.compile(r'"model"\s*:\s*"claude-[^"]*"')
body_str = pattern.sub(f'"model": "{target_model}"', body_str)
# forward with requests.stream=True
```

1. 拦截 HTTP 请求体
2. 正则匹配所有 `"model": "claude-*"` 并替换
3. 流式转发到上游，支持 SSE（Server-Sent Events）

## FAQ

**Q: 为什么不用 cc-switch 的模型路由？**
A: cc-switch 的 `claudeDesktopModelRoutes` 只对 Claude Desktop (GUI) 请求生效，Claude CLI 请求不走这个映射。

**Q: 为什么不用 Claude Code 的 modelOverrides？**
A: Claudian 调用 Claude CLI 时传了 `--model` 参数，优先级高于 settings.json 中的 `modelOverrides`。

**Q: 支持哪些平台？**
A: 所有兼容 Anthropic Messages API 的平台都支持，包括 StepFun、SiliconFlow、DeepSeek、OpenRouter 等。

## License

MIT
*（内容由AI生成，仅供参考）*
*（内容由AI生成，仅供参考）*
