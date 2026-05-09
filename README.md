# DeepSeek V4 Proxy for OpenAI Codex

本地 HTTP 代理，将 OpenAI Codex 的 API 请求透明转发到 DeepSeek API (`deepseek-v4-pro`)，自动改写 model 名称，并完成 Responses ↔ Chat Completions 格式互转。

## 目录

- [工作原理](#工作原理)
- [快速开始](#快速开始)
- [配置环境变量](#配置环境变量)
- [Codex 集成](#codex-集成)
- [API 端点](#api-端点)
- [验证](#验证)
- [SSL 配置](#ssl-配置)
- [License](#license)

## 工作原理

```
Codex App ──(Responses API)──▶ Proxy :9095 ──(Chat Completions)──▶ DeepSeek API
                   ◀── SSE / JSON ──                ◀── SSE / JSON ──
```

- 代理监听 `http://0.0.0.0:9095`，拦截 Codex 发来的 `/v1/responses` 或 `/v1/chat/completions` 请求
- 将 Responses 格式转换为 Chat Completions，model 字段改写为 `deepseek-v4-pro`
- 转发到 DeepSeek API，再将响应转回原始格式后返回

## 快速开始

**依赖**：Python 3.10+, aiohttp, certifi

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量（二选一）
cp .env.example .env && vim .env          # 方式 A：.env 文件（推荐）
export DEEPSEEK_API_KEY="sk-..."          # 方式 B：直接 export

# 3. 启动代理
python proxy.py
```

代理启动后监听 `http://0.0.0.0:9095`，健康检查：`curl http://localhost:9095/health`

## 配置环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `DEEPSEEK_API_KEY` | 是 | - | DeepSeek API Key |
| `TARGET_MODEL` | 否 | `deepseek-v4-pro` | 转发后的目标模型名 |
| `LISTEN_HOST` | 否 | `0.0.0.0` | 代理监听地址 |
| `LISTEN_PORT` | 否 | `9095` | 代理监听端口 |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com` | DeepSeek API 地址 |
| `DEEPSEEK_PROXY_KEY` | 否 | - | Codex provider 配置用，代理不校验，设任意值即可 |

> 项目根目录的 `.env.example` 包含了所有可配变量的模板，可直接复制使用。

## Codex 集成

编辑 `~/.codex/config.toml`，加入以下配置：

```toml
# 模型 & provider — 放在文件顶层
model = "deepseek-v4-pro"
model_provider = "deepseek-proxy"

# Provider 定义
[model_providers.deepseek-proxy]
name = "DeepSeek V4 Proxy"
base_url = "http://localhost:9095/v1"
env_key = "DEEPSEEK_PROXY_KEY"
wire_api = "responses"
```

然后设置环境变量并重启 Codex：

```bash
export DEEPSEEK_PROXY_KEY="any-value"   # 任意非空字符串即可
```

> **说明**：新版 Codex 要求 `wire_api = "responses"`。代理内部会将 Responses 格式请求转换为 `/v1/chat/completions` 转发给 DeepSeek，再将响应转回 Responses 格式。

### 备选：仅使用 Chat Completions

如果你使用的是旧版 Codex 或其他 OpenAI 兼容客户端，可以跳过格式转换，直接走 Chat Completions：

```toml
[model_providers.deepseek-proxy]
name = "DeepSeek V4 Proxy"
base_url = "http://localhost:9095/v1"
env_key = "DEEPSEEK_PROXY_KEY"
# wire_api 不设置，或设为空字符串
```

此时客户端直接调用 `/v1/chat/completions`，代理透传请求。

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/chat/completions` | 对话补全（流式 + 非流式），透传到 DeepSeek |
| `POST` | `/v1/responses` | Responses API，转换为 Chat Completions 后转发 |
| `GET` | `/v1/models` | 模型列表，透传到 DeepSeek |
| `GET` | `/health` | 健康检查 |

## 验证

```bash
# 健康检查
curl http://localhost:9095/health

# Chat Completions 端点
curl http://localhost:9095/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'

# Responses 端点
curl http://localhost:9095/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","input":"hello"}'
```

## SSL 配置

仅在内网开发或代理环境下需要调整，正常情况下无需配置。

| 变量 | 说明 |
|------|------|
| `SSL_CA_BUNDLE` | 自定义 CA 证书路径（默认使用 certifi 内置证书） |
| `SSL_NO_VERIFY` | 设为 `1` / `true` / `yes` 跳过 SSL 验证 |

## License

MIT
