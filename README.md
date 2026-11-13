# TokenFactory Reverse Proxy (`tfproxy`)

面向 `https://tokenfactory.kt.com/` 的本地网关，同时提供 **OpenAI** 与 **Anthropic** 两种兼容协议：
`/v1/chat/completions`、`/v1/messages`（Claude Code 可用 `ANTHROPIC_BASE_URL` 直连）、`/v1/models`。
上游走 TokenFactory Web API（cookie 会话 + turns/conversations 协议）。

> 请求流：客户端 → 本网关（协议转换/工具调用/配额轮换）→ SOCKS5 出口 → TokenFactory。
> 全链路除 reCAPTCHA token 需要浏览器产出外均为纯协议。

## 功能

- **双协议兼容**
  - OpenAI：`POST /v1/chat/completions`（stream / non-stream）、`GET /v1/models`
  - Anthropic：`POST /v1/messages`（stream / non-stream）、`/v1/messages/count_tokens`
- **工具调用（function calling）**
  - 上游推理模型禁用原生函数工具，代理层通过 JSON 合同 + `response_format=json_object` 实现（详见 USAGE.md）
  - 输出转标准 `tool_calls` / `tool_use`；`tool` / `tool_result` 多轮循环可用
  - 解析容错：JSON 修复、`<think>` 剥离、` ```tool_call ` 围栏兜底（借鉴 llm-tool-capability）
- **账号池与配额**
  - 每账号每日调用计数（上游限额 10 次/天），用完自动纯协议换号
  - 上游 429/CH-208 自动标记耗尽并轮换重试
  - ACCESS_TOKEN 到期前自动纯协议刷新（`/internal/auth/refresh`）
  - REFRESH_TOKEN（14 天寿命）按号独立巡检，临期自动起浏览器重登（并发上限 2）
- **注册机（一条命令）**
  - `scripts/register_and_import.py --target N --batch-size M`：注册→邮箱验证→登录→自动入池
  - 增量落盘，中断不丢号；任何 signup 失败自动换 token 重试
- **上游适配**
  - Cookie 会话模式（协议登录导出，无需人工）
  - TokenFactory Web Chat 协议：turns / conversations / generations completions（SSE/JSON）
  - 自动发现 tenant/workspace（从 `tf_auth_profile` cookie 解析）
- **模型**
  - 公开模型目录同步（73 模型）与别名映射（`claude-*` → 指定上游模型）
- **网络与运维**
  - SOCKS5/SOCKS5H 上游代理
  - 请求/上游超时、重试与错误映射
  - 结构化日志、可选管理端点

## 目录结构

```text
tokenfactory_proxy/
  tfproxy/
    __init__.py
    __main__.py          # CLI 入口
    config.py            # 配置加载与校验
    logging_setup.py
    errors.py
    models.py            # OpenAI 兼容数据模型
    state.py             # 运行态
    server.py            # FastAPI app
    routes/
      openai.py          # /v1/*
      admin.py           # /health, /admin/*
    services/
      account_pool.py
      chat.py            # OpenAI <-> TokenFactory 适配
      models.py          # 模型目录缓存
    upstream/
      http.py            # httpx 客户端、cookie/代理
      auth.py            # 登录与加密
      client.py          # TokenFactory Web API 客户端
      protocol.py        # turns/completions SSE 解析
  config.example.yaml
  requirements.txt
  pyproject.toml
  data/                  # cookies/state 持久化（运行生成）
  logs/
```

## 快速开始

### 1) 安装依赖

```powershell
cd C:\Users\39455\workspace\tokenfactory_proxy
python -m pip install -r requirements.txt
# 如需自动获取 reCAPTCHA / 浏览器登录辅助：
python -m pip install playwright DrissionPage
python -m playwright install chromium
```

### 2) 准备配置与会话

```powershell
copy config.example.yaml config.yaml
```

编辑 `config.yaml`：

- `server.api_keys`: 设置下游调用本代理时使用的 key
- `upstream.proxy`: 设置可访问 `tokenfactory.kt.com` 的 SOCKS5 出口（如不需要可留空）
- `upstream.cookies_file`: 指向已登录会话的 cookies 文件（推荐 JSON 导出，格式见下）
- `upstream.tenant_id` / `upstream.workspace_id`: 可选；留空时代理会尝试从 console 发现

**cookies.json 示例：**

```json
{
  "cookies": [
    {"name": "SESSION", "value": "...", "domain": "tokenfactory.kt.com", "path": "/"}
  ],
  "url": "https://tokenfactory.kt.com/console/<tenant>/workspace/<workspace>/playground"
}
```

也支持直接传入浏览器导出的 cookie 数组，或简单的 `{"cookie_name": "value"}` 映射。

### 3) 启动

```powershell
python -m tfproxy -c config.yaml
# 或
python run.py -c config.yaml
```

默认监听 `http://0.0.0.0:8000`。

### 4) 调用示例

```bash
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer sk-tokenfactory-local-change-me"

curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-tokenfactory-local-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "stream": true,
    "messages": [{"role":"user","content":"你好，请用一句话介绍你自己"}]
  }'
```

OpenAI Python SDK：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-tokenfactory-local-change-me")
resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "ping"}],
    stream=True,
)
for chunk in resp:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

## 配置说明

| 配置项 | 说明 |
|---|---|
| `server.host/port` | 监听地址 |
| `server.api_keys` | 下游访问密钥列表；为空且 `allow_anonymous=false` 时拒绝 |
| `upstream.base_url` | 上游站点，默认 `https://tokenfactory.kt.com` |
| `upstream.proxy` | 上游 HTTP(S) 代理，支持 `socks5://` / `socks5h://` |
| `upstream.cookies_file` | 会话 cookies 来源 |
| `upstream.tenant_id/workspace_id` | 控制台租户/工作区上下文；空则自动发现 |
| `upstream.chat_mode` | `auto` / `turns` / `openai` |
| `upstream.reuse_conversation` | 是否在多次请求间复用同一 conversation |
| `models.aliases` | 模型别名与默认模型（`*`） |
| `accounts[]` | 账号池，用于登录/刷新会话 |
| `auth.recaptcha_mode` | reCAPTCHA 获取策略：`playwright` / `manual` / `disabled` |

## 账号与登录

- **推荐**：先在浏览器完成登录与必要验证，将 cookies 导出到 `upstream.cookies_file`，由代理直接复用会话（最稳定）。
- **账号池登录**：在 `accounts` 配置邮箱/密码后，代理可调用登录流程：
  1. `GET /api/v1/account/login/public-key` 获取 RSA 公钥
  2. 使用 `RSA-OAEP(SHA-256)` 加密密码
  3. 获取 reCAPTCHA token（`recaptcha_mode=playwright` 时通过浏览器页面执行 `grecaptcha.execute`）
  4. `POST /api/v1/account/login` 建立会话并持久化 cookies
- 注册流程（验证码邮箱、terms payload、`/api/v1/account/signup`）在 `tfproxy.upstream.auth` / 现有 `tokenfactory/tools/` 组件中提供接口封装，可按需启用自动注册补号。

## 上游协议说明

当前实现基于已观测到的 TokenFactory Web 端点族：

- `GET /api/v1/chat/public-models`、`GET /api/v1/chat/model-filter-options`
- `GET /api/v1/chat/quota`
- `GET/PUT /api/v1/chat/conversations...`
- `POST /api/v1/chat/turns`（新对话 turn）
- `POST /api/v1/chat/conversations/{conversationId}/turns`（追加 turn）
- `POST /api/v1/chat/conversations/{conversationId}/turns/{turnId}/generations/{generationId}/completions`（流式/生成结果）

由于 turns/completions 的精确 payload 与 SSE 事件在不同版本前端可能存在差异，`tfproxy/upstream/protocol.py` 与 `client.py` 将请求构造与响应解析集中管理，并记录原始上游事件以便调试；可通过 `log_level: debug` 观察并按需微调字段映射。

## 故障排查

- `401/403`：下游 key 不匹配或上游会话失效，检查 `server.api_keys` 与 `cookies_file`；必要时重新导出 cookies。
- `502 upstream error`：查看日志中的上游状态码/响应体；确认 `upstream.proxy` 可达且能访问 `tokenfactory.kt.com`。
- 模型为空：确认 cookies 有效且能访问 `/api/v1/chat/public-models`；或检查代理出口/地区限制。
- 流式中断：提高 `upstream.read_timeout_seconds`，并在 debug 日志中确认 completions 端点返回的 content-type 与事件格式。

## 注意

本项目用于在已获授权的前提下对自有账号/测试环境进行接口适配与自动化集成。请遵守目标平台服务条款与适用法律，仅在授权范围内使用。
