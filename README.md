# DeepSeek V4 Proxy for OpenAI Codex

本地 HTTP 代理，将 codex app 的 OpenAI 格式请求转发到 DeepSeek API (`deepseek-v4-pro`)，自动改写 model 名称。

## 快速开始

```bash
pip install -r requirements.txt
export DEEPSEEK_API_KEY="sk-your-deepseek-api-key"
python proxy.py
```

代理默认监听 `http://0.0.0.0:9095`。

## Codex 配置

编辑 `~/.codex/config.toml`，添加一个指向本机代理的 provider，然后切换过去。

### 1. 添加 provider

```toml
[model_providers.deepseek-proxy]
name = "DeepSeek V4 Proxy"
base_url = "http://localhost:9095/v1"
env_key = "DEEPSEEK_PROXY_KEY"
wire_api = "responses"
```

`env_key` 对应的环境变量设为任意非空字符串（代理不校验）：

```bash
export DEEPSEEK_PROXY_KEY="any-value"
```

### 2. 切换模型

```toml
model = "deepseek-v4-pro"
model_provider = "deepseek-proxy"
openai_base_url = "http://localhost:9095/v1"
```

> 新版 Codex 要求 `wire_api = "responses"`，代理内部会转换为 `/v1/chat/completions` 格式转发给 DeepSeek，再将响应转回 Responses 格式。

### 3. 完整 config.toml 示例

```toml
# 模型 & provider
model = "deepseek-v4-pro"
model_provider = "deepseek-proxy"
openai_base_url = "http://localhost:9095/v1"

# Provider 定义
[model_providers.deepseek-proxy]
name = "DeepSeek V4 Proxy"
base_url = "http://localhost:9095/v1"
env_key = "DEEPSEEK_PROXY_KEY"
wire_api = "responses"

```

改完后重启 Codex 即可生效。

## 环境变量

- `DEEPSEEK_API_KEY` (必填) — DeepSeek API Key
- `TARGET_MODEL` (默认 deepseek-v4-pro) — 目标模型名
- `LISTEN_PORT` (默认 9095) — 代理监听端口
- `LISTEN_HOST` (默认 0.0.0.0) — 代理监听地址
- `DEEPSEEK_BASE_URL` (默认 https://api.deepseek.com)
- `SSL_CA_BUNDLE` — 自定义 CA 证书路径（默认使用 certifi）
- `SSL_NO_VERIFY` — 设为 `1`/`true`/`yes` 跳过 SSL 验证（仅内网开发）

## 端点

- `POST /v1/chat/completions` — 对话补全（流式 + 非流式），透传请求到 DeepSeek
- `POST /v1/responses` — Responses API，转换为 Chat Completions 格式后转发
- `GET /v1/models` — 模型列表
- `GET /health` — 健康检查

## 验证

```bash
# 健康检查
curl http://localhost:9095/health

# Chat Completions 端点
curl http://localhost:9095/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'

# Responses 端点（Codex wire_api = "responses" 使用）
curl http://localhost:9095/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","input":"hello"}'
```
