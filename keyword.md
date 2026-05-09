# DeepSeek V4 Proxy 格式转换要点

## 核心架构：两层协议转换

```
Codex (Responses API)  ──▶  Proxy :9095  ──▶  DeepSeek (Chat Completions)
        ◀──  Responses SSE/JSON  ──          ◀──  Chat SSE/JSON  ──
```

代理做了两个方向的转换：**请求方向**（Responses → Chat）和**响应方向**（Chat → Responses）。同时还支持 Chat Completions 的透传（含轻量规范化）。

---

## 一、请求方向：Responses → Chat Completions

函数 `_responses_to_chat()` 负责此转换，要点如下：

### 1. `input` → `messages` 的三种处理路径

| `input` 类型 | 转换策略 |
|---|---|
| **字符串** | 直接包装为单条 `{"role": "user", "content": inp}` |
| **list[dict]，每项含 `"role"` 键** | 逐项保留 role，content 经 `_normalize_content` 处理 |
| **list[dict]，每项含 `"type": "message"`** | 提取 `content` 数组中每个 part 的 `text` 字段，用 `\n` 拼接后作为单条消息 |
| **其他** | `str(item)` 后包装为 user 消息 |

### 2. 字段映射

| Responses 字段 | Chat Completions 字段 | 说明 |
|---|---|---|
| `model` | `TARGET_MODEL` | 强制改写为配置的目标模型 |
| `input` | `messages` | 如上表，多条规则 |
| `stream` | `stream` | 原样透传 |
| `max_output_tokens` | `max_tokens` | 字段名更换 |
| `temperature` | `temperature` | 原样透传 |
| `top_p` | `top_p` | 原样透传 |

### 3. ⚠️ 未映射的关键字段

- **`instructions`** — Responses API 的系统提示词，**未转发**到 Chat 的 `messages` 中
- **`tools` / `tool_choice`** — 工具调用定义完全不支持
- **`previous_response_id`** — 对话历史延续不支持
- **`metadata` / `store`** — 忽略

---

## 二、Role 与 Content Type 规范化

### Role 映射（`_normalize_role`）

| Codex/OpenAI Role | DeepSeek Role |
|---|---|
| `developer` | `system` |
| 其他（`user`, `assistant`, `tool`） | 保持原样 |

### Content Type 映射（`_normalize_content`）

| 输入 type | 规范化后 type |
|---|---|
| `input_text` | `text` |
| `output_text` | `text` |
| 字符串 content | 原样返回 |
| 其他 type | 保持不变 |

> 注意：`image_url`、`file` 等非文本 content part 目前**没有处理**，可能被透传但 DeepSeek 未必支持。

---

## 三、响应方向：Chat Completions → Responses（非流式）

函数 `_chat_to_responses()` 负责此转换：

### 字段映射

| Chat 响应字段 | Responses 响应字段 |
|---|---|
| `id`（或 `request_id`） | `id` |
| — | `object: "response"`（硬编码） |
| `created` | `created_at` |
| — | `status: "completed"`（硬编码） |
| `model` | `model` |
| `choices[0].message.content` | `output[0].content[0].text` |
| — | `output[0].type: "message"`, `role: "assistant"` |

### Usage 映射

| Chat usage 字段 | Responses usage 字段 |
|---|---|
| `prompt_tokens` | `input_tokens` |
| `completion_tokens` | `output_tokens` |
| `total_tokens` | `total_tokens` |
| — | `input_tokens_details.cached_tokens: 0`（硬编码为 0） |
| — | `output_tokens_details.reasoning_tokens: 0`（硬编码为 0） |

> ⚠️ `cached_tokens` 和 `reasoning_tokens` 被硬编码为 0，即使 DeepSeek 实际返回了这些值也不会体现。

---

## 四、流式响应转换：最复杂的部分

`_relay_responses_stream()` 将 DeepSeek 的 SSE 流转换为 OpenAI Responses API 规范的 9 步事件序列：

| 序号 | SSE Event | 含义 |
|---|---|---|
| 1 | `response.created` | 创建 response 对象，状态 `in_progress` |
| 2 | `response.in_progress` | 状态更新 |
| 3 | `response.output_item.added` | 声明即将输出的 message item |
| 4 | `response.content_part.added` | 声明 message 中的空 text content part |
| 5 | `response.output_text.delta` | **循环**：每个 token 增量文本 |
| 6 | `response.output_text.done` | text part 完成，附完整文本 |
| 7 | `response.content_part.done` | content part 完成 |
| 8 | `response.output_item.done` | message item 完成 |
| 9 | `response.completed` | 整个 response 完成，附完整 output 摘要 |

**流式解析细节**：
- 逐行拆分上游 SSE，以 `data:` 前缀识别事件
- 遇到 `data: [DONE]` 时停止
- 从 `choices[0].delta.content` 中取增量文本
- 错误容忍：JSON 解析异常会 `continue` 跳过，不中断流

---

## 五、Chat Completions 透传模式

当客户端直接调用 `/v1/chat/completions` 时，只做轻量规范化：

1. **model 改写**：替换为 `TARGET_MODEL`
2. **messages 规范化**：每条消息的 role 经 `_normalize_role`，content 经 `_normalize_content`
3. **流式**：SSE 原始字节级透传，不做任何转换
4. **非流式**：JSON 整体透传

---

## 六、边界条件与限制总结

| 要点 | 说明 |
|---|---|
| **instructions 丢失** | Responses API 的 system prompt 未转发，这是最大的功能缺口 |
| **工具调用不支持** | `tools`、`tool_choice` 等相关字段完全未映射 |
| **单轮对话为主** | `previous_response_id` 不处理，依赖客户端在 `input` 中自行拼装历史 |
| **reasoning_tokens 丢失** | usage 中硬编码为 0，无法反映 DeepSeek 的思维链消耗 |
| **非文本 content 无处理** | `image_url`、`file` 等类型原样透传 |
| **流式容错** | JSON 解析失败时跳过该 chunk 继续，不会中断连接 |
| **上游错误透传** | DeepSeek 返回 ≥400 状态码时，body 原文透传给客户端 |
| **model 名强制改写** | 无论客户端发什么 model 名，一律替换为 `TARGET_MODEL` |
| **SSL 灵活配置** | 支持自定义 CA 证书（`SSL_CA_BUNDLE`）和跳过验证（`SSL_NO_VERIFY`） |

---

## 七、数据流全景

```
请求入口
├── POST /v1/responses ──► _responses_to_chat()
│   ├── stream=false ──► POST DeepSeek ──► _chat_to_responses()
│   └── stream=true  ──► POST DeepSeek ──► _relay_responses_stream() (9步SSE事件)
│
├── POST /v1/chat/completions ──► model改写 + role/content规范化
│   ├── stream=false ──► POST DeepSeek ──► 透传JSON
│   └── stream=true  ──► POST DeepSeek ──► 透传SSE字节
│
├── GET /v1/models ──► 透传
└── GET /health ──► {"status": "ok"}
```

核心就一句话：这个代理本质上是一个 **Responses API 到 Chat Completions 的不完全降级适配层**，覆盖了最常用的纯文本对话场景，但系统提示词、工具调用、多模态、对话延续等高级特性均未处理。
