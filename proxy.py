#!/usr/bin/env python3
"""
DeepSeek V4 Pro Proxy for OpenAI Codex
=======================================
本地 HTTP 代理，将 OpenAI 兼容的 API 请求转发到 DeepSeek API，
自动将请求中的 model 字段改写为目标 DeepSeek 模型。

用法:
    export DEEPSEEK_API_KEY="sk-..."
    python proxy.py

环境变量:
    DEEPSEEK_API_KEY  - DeepSeek API key（必填）
    TARGET_MODEL      - 目标模型名（默认: deepseek-v4-pro）
    LISTEN_HOST       - 监听地址（默认: 0.0.0.0）
    LISTEN_PORT       - 监听端口（默认: 9095）
    DEEPSEEK_BASE_URL - DeepSeek API 基础 URL（默认: https://api.deepseek.com）
"""

import asyncio
import json
import logging
import os
import ssl
import sys
import time

import certifi
from aiohttp import ClientSession, ClientTimeout, ClientError, TCPConnector
from aiohttp import web

# ── 配置 ────────────────────────────────────────────────────────────────

DEEPSEEK_BASE_URL = os.getenv(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
).rstrip("/")
# 执行 export DEEPSEEK_API_KEY=sk_*****，通过os.getenv("DEEPSEEK_API_KEY", "")获取，
# 只能使用python3 proxy.py启动时才能获取到
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
TARGET_MODEL = os.getenv("TARGET_MODEL", "deepseek-v4-pro")
LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "9095"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("deepseek-proxy")

if not DEEPSEEK_API_KEY:
    log.fatal("DEEPSEEK_API_KEY 环境变量未设置")
    sys.exit(1)

# ── HTTP 客户端 ──────────────────────────────────────────────────────────

# SSL 配置
SSL_NO_VERIFY = os.getenv("SSL_NO_VERIFY", "").lower() in ("1", "true", "yes")
SSL_CA_BUNDLE = os.getenv("SSL_CA_BUNDLE", "")  # 自定义 CA 证书路径


def _make_ssl_context() -> ssl.SSLContext:
    """创建 SSL context：优先使用环境变量指定的 CA，其次 certifi，最后系统默认."""
    if SSL_NO_VERIFY:
        log.warning("SSL verification disabled (SSL_NO_VERIFY=true)")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    cafile = SSL_CA_BUNDLE or certifi.where()
    ctx = ssl.create_default_context(cafile=cafile)
    log.info("SSL CA bundle: %s", cafile)
    return ctx


def _make_connector() -> TCPConnector:
    """创建带自定义 SSL context 的连接器."""
    return TCPConnector(limit=50, ttl_dns_cache=300, ssl=_make_ssl_context())


def _make_session(timeout: ClientTimeout = None) -> ClientSession:
    """创建带 SSL 配置的 ClientSession."""
    if timeout is None:
        timeout = ClientTimeout(total=600, connect=10)
    return ClientSession(connector=_make_connector(), timeout=timeout)


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _mask_key(key: str) -> str:
    """遮蔽 API key 用于日志输出."""
    if len(key) <= 10:
        return "***"
    return key[:7] + "..." + key[-4:]


# ── Responses ↔ Chat Completions 格式转换 ───────────────────────────────

# DeepSeek 支持的 roles: system, user, assistant, tool, latest_reminder
# OpenAI/Codex 额外 roles 需要映射
_ROLE_MAP = {
    "developer": "system",
}

# Content part type 映射（OpenAI Responses API → DeepSeek Chat Completions）
_CONTENT_TYPE_MAP = {
    "input_text": "text",
    "output_text": "text",
}


def _normalize_role(role: str) -> str:
    """将 Codex 专用 role 映射为 DeepSeek 支持的 role."""
    return _ROLE_MAP.get(role, role)


def _normalize_content(content):
    """规范化 message content：将 input_text/output_text 类型映射为 text，保持字符串不变."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        result = []
        for part in content:
            if isinstance(part, dict):
                part_type = part.get("type", "")
                normalized_type = _CONTENT_TYPE_MAP.get(part_type, part_type)
                result.append({**part, "type": normalized_type})
            else:
                result.append(part)
        return result
    return content


def _responses_to_chat(body: dict) -> dict:
    """将 Responses API 请求转换为 Chat Completions 请求."""
    inp = body.get("input", "")
    if isinstance(inp, str):
        messages = [{"role": "user", "content": inp}]
    elif isinstance(inp, list):
        messages = []
        for item in inp:
            if isinstance(item, dict) and "role" in item:
                messages.append({"role": _normalize_role(item["role"]), "content": _normalize_content(item.get("content", ""))})
            elif isinstance(item, dict) and item.get("type") == "message":
                # Responses format: {type: "message", role: "...", content: [...]}
                text_parts = []
                for part in item.get("content", []):
                    if isinstance(part, dict):
                        text_parts.append(part.get("text", ""))
                    else:
                        text_parts.append(str(part))
                messages.append({"role": _normalize_role(item.get("role", "user")), "content": "\n".join(text_parts)})
            else:
                messages.append({"role": "user", "content": str(item)})
    else:
        messages = [{"role": "user", "content": str(inp)}]

    return {
        "model": TARGET_MODEL,
        "messages": messages,
        "stream": body.get("stream", False),
        "max_tokens": body.get("max_output_tokens"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
    }


def _chat_to_responses(chat_data: dict, request_id: str = None) -> dict:
    """将 Chat Completions 响应转换为 Responses API 格式."""
    import uuid
    choice = (chat_data.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")

    usage = chat_data.get("usage", {})
    resp = {
        "id": request_id or chat_data.get("id", ""),
        "object": "response",
        "created_at": chat_data.get("created", 0),
        "status": "completed",
        "model": chat_data.get("model", TARGET_MODEL),
        "output": [
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": content,
                        "annotations": [],
                    }
                ],
            }
        ],
    }
    if usage:
        resp["usage"] = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
    return resp


# ── 代理处理器 ───────────────────────────────────────────────────────────

async def handle_responses(request):
    """代理 POST /v1/responses — 转换 Responses 格式为 Chat Completions."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    original_model = body.get("model", "unknown")
    is_stream = body.get("stream", False)
    chat_body = _responses_to_chat(body)

    log.info(
        "responses -> chat  model: %s -> %s  stream: %s  messages: %d",
        original_model, TARGET_MODEL, is_stream,
        len(chat_body.get("messages", [])),
    )

    timeout = ClientTimeout(total=600, connect=10)
    try:
        async with _make_session(timeout) as sess:
            upstream = await sess.post(
                f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
                json=chat_body,
                headers=_auth_headers(),
            )
            try:
                if upstream.status >= 400:
                    error_body = await upstream.text()
                    log.error("DeepSeek error %d: %s",
                              upstream.status, error_body[:500])
                    return web.Response(
                        text=error_body, status=upstream.status,
                        headers={"Content-Type": "application/json"},
                    )
                if is_stream:
                    return await _relay_responses_stream(request, upstream)
                else:
                    data = await upstream.json()
                    return web.json_response(_chat_to_responses(data))
            finally:
                if not is_stream:
                    upstream.release()
    except ClientError as e:
        log.error("Upstream connection failed: %s", e)
        return web.json_response(
            {"error": f"Upstream connection failed: {e}"}, status=502)
    except asyncio.TimeoutError:
        log.error("Request timeout")
        return web.json_response(
            {"error": "Upstream request timed out"}, status=504)


async def handle_chat_completions(request):
    """代理 POST /v1/chat/completions — 同时支持流式和非流式."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    original_model = body.get("model", "unknown")
    body["model"] = TARGET_MODEL
    is_stream = body.get("stream", False)

    # 规范化 messages 中的 role 和 content type
    for msg in body.get("messages", []):
        if "role" in msg:
            msg["role"] = _normalize_role(msg["role"])
        if "content" in msg:
            msg["content"] = _normalize_content(msg["content"])

    log.info(
        "chat/completions  model: %s -> %s  stream: %s  messages: %d",
        original_model, TARGET_MODEL, is_stream,
        len(body.get("messages", [])),
    )

    timeout = ClientTimeout(total=600, connect=10)
    try:
        async with _make_session(timeout) as sess:
            upstream = await sess.post(
                f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
                json=body,
                headers=_auth_headers(),
            )
            try:
                if upstream.status >= 400:
                    error_body = await upstream.text()
                    log.error("DeepSeek error %d: %s",
                              upstream.status, error_body[:500])
                    return web.Response(
                        text=error_body, status=upstream.status,
                        headers={"Content-Type": "application/json"},
                    )
                if is_stream:
                    return await _relay_stream(request, upstream)
                else:
                    return await _relay_json(upstream)
            finally:
                if not is_stream:
                    upstream.release()
    except ClientError as e:
        log.error("Upstream connection failed: %s", e)
        return web.json_response(
            {"error": f"Upstream connection failed: {e}"}, status=502)
    except asyncio.TimeoutError:
        log.error("Request timeout")
        return web.json_response(
            {"error": "Upstream request timed out"}, status=504)


async def handle_models(request):
    """代理 GET /v1/models."""
    timeout = ClientTimeout(total=30)
    try:
        async with _make_session(timeout) as sess:
            async with sess.get(
                f"{DEEPSEEK_BASE_URL}/v1/models",
                headers=_auth_headers(),
            ) as upstream:
                data = await upstream.json()
                return web.json_response(data, status=upstream.status)
    except ClientError as e:
        log.error("Models error: %s", e)
        return web.json_response({"error": str(e)}, status=502)


async def handle_health(request):
    """健康检查."""
    return web.json_response({"status": "ok", "target_model": TARGET_MODEL})


# ── Responses 流式中继 ───────────────────────────────────────────────────

async def _relay_responses_stream(request, upstream):
    """将 Chat Completions SSE 流转换为完整的 Responses SSE 事件序列."""
    import uuid
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    resp = web.StreamResponse()
    resp.headers["Content-Type"] = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    await resp.prepare(request)

    def _sse(event_type: str, data: dict):
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()

    now = int(time.time())

    # 1. response.created
    await resp.write(_sse("response.created", {
        "type": "response.created",
        "response": {
            "id": resp_id, "object": "response", "created_at": now,
            "status": "in_progress", "model": TARGET_MODEL, "output": [],
        },
    }))

    # 2. response.in_progress
    await resp.write(_sse("response.in_progress", {
        "type": "response.in_progress",
        "response": {"id": resp_id, "object": "response", "status": "in_progress"},
    }))

    # 3. response.output_item.added — 声明即将输出的 message item
    await resp.write(_sse("response.output_item.added", {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "type": "message", "id": msg_id, "status": "in_progress",
            "role": "assistant", "content": [],
        },
    }))

    # 4. response.content_part.added — 声明 message 中的 text content part
    await resp.write(_sse("response.content_part.added", {
        "type": "response.content_part.added",
        "item_id": msg_id, "output_index": 0, "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    }))

    accumulated = []
    done = False
    try:
        async for raw_chunk in upstream.content.iter_any():
            text = raw_chunk.decode(errors="replace")
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        done = True
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        accumulated.append(content)
                        # 5. response.output_text.delta
                        await resp.write(_sse("response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "item_id": msg_id, "output_index": 0,
                            "content_index": 0, "delta": content,
                        }))
            if done:
                break
    except Exception as e:
        log.warning("Responses stream interrupted: %s", e)
    finally:
        full_text = "".join(accumulated)
        # 6. response.output_text.done
        await resp.write(_sse("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": msg_id, "output_index": 0, "content_index": 0,
            "text": full_text,
        }))
        # 7. response.content_part.done
        await resp.write(_sse("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": msg_id, "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": full_text, "annotations": []},
        }))
        # 8. response.output_item.done
        await resp.write(_sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message", "id": msg_id, "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": full_text, "annotations": []}],
            },
        }))
        # 9. response.completed
        await resp.write(_sse("response.completed", {
            "type": "response.completed",
            "response": {
                "id": resp_id, "object": "response",
                "created_at": int(time.time()), "status": "completed",
                "model": TARGET_MODEL,
                "output": [
                    {
                        "type": "message", "id": msg_id, "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": full_text, "annotations": []}],
                    }
                ],
            },
        }))
        await resp.write_eof()
    return resp


# ── 中继辅助函数 ─────────────────────────────────────────────────────────

async def _relay_stream(request, upstream):
    """逐块中继 SSE 流式响应."""
    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Connection"] = "keep-alive"
    await response.prepare(request)
    try:
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
    except Exception as e:
        log.warning("Stream interrupted: %s", e)
    finally:
        await response.write_eof()
    return response


async def _relay_json(upstream):
    """中继非流式 JSON 响应."""
    data = await upstream.json()
    return web.json_response(data, status=upstream.status)


# ── 应用 & 入口 ──────────────────────────────────────────────────────────

def create_app():
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/v1/responses", handle_responses)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app


def main():
    app = create_app()
    log.info("DeepSeek V4 Proxy on %s:%d -> %s (model: %s, key: %s)",
             LISTEN_HOST, LISTEN_PORT, DEEPSEEK_BASE_URL,
             TARGET_MODEL, _mask_key(DEEPSEEK_API_KEY))
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()
