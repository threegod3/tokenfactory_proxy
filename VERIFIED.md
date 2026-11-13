# TokenFactory 实测记录（2026-11-12）

## 是否纯协议？—— 实测答案

**除 reCAPTCHA token 的产出外，全流程都是纯协议（curl + socks5），不需要浏览器参与任何 POST。**

这一点用对照实验证实（`tokenfactory/tools/test_signup_pure.py`）：

| 变体 | 方式 | 结果 |
|---|---|---|
| A | 纯 curl POST signup（无浏览器 cookie，只带页面产出的 token） | **201 成功** |
| B | 纯 curl + 浏览器 cookie | 未触发（A 已成功） |
| C | 页面内 fetch（旧方案） | 未触发（A 已成功） |

早期判断“必须页面内 fetch”是错的 —— 失败的真正原因不是“浏览器上下文”，
而是 `PBG-019` 缺少 `tf-tenant-id` / `tf-space-id` 头，以及 reCAPTCHA 分数。

### 唯一绕不开的浏览器环节

`grecaptcha.enterprise.execute()` 只能在真实浏览器里执行（Google 的客户端 JS + 设备指纹）。
所以浏览器只做一件事：**产出一个 token 字符串**，产出后立即关闭，POST 全部由 curl 完成。

token 是一次性的、约 2 分钟有效，因此每次注册/登录都需要新 token。

### 全自动一步到位

```powershell
cd C:\Users\39455\workspace\tokenfactory_proxy
python scripts\register_and_import.py --count 1
```

实测输出（2026-11-12 23:17）：

```
=== register 1/1 (pure protocol) ===
  mail=<account1>@example.com aid=1865
    signup attempt 1: 201 {"signupRequestId":"34c44f24-...","verificationRequired":true}
    code=912226
    confirmed: active
    login attempt 1: 200 {"ok":true,"invitations":[]}
  imported -> data/cookies.json
DONE: registered=1/1, logged_in=1/1
```

随后该号经反代机调用 `gpt-6-astra`，返回：

> HTTPS 在 HTTP 的基础上通过 TLS 加密通信并验证服务器身份，能防止数据被窃听或篡改，而 HTTP 默认以明文传输数据。

`usage: prompt 4 / completion 16 / total 20`，HTTP 200。

## 结论：链路全部打通

| 环节 | 状态 | 证据 |
|---|---|---|
| 注册机（新号注册） | ✅ | `<account3>@example.com` 收码 `------` → `stage: done` |
| 纯协议登录 | ✅ | `POST /internal/auth/login` → 200，拿到 ACCESS/REFRESH token |
| 反代机启动 | ✅ | `http://127.0.0.1:8000/health` 200，模型目录 73 个 |
| `gpt-6-astra` 非流式 | ✅ | 返回 `ASTRA_OK` / 正常答案，HTTP 200 |
| `gpt-6-astra` 流式 | ✅ | SSE 逐段输出 + `finish_reason: stop` + `[DONE]` |

## 关键协议（逆向结论）

### 1. 登录（纯协议，reCAPTCHA token 必须由真浏览器产出）

```
GET  /api/v1/auth/encryption-key
     → {"keyPairId":"v1","publicKey":{...RSA JWK, alg RSA-OAEP-256}}

密码加密：RSA-OAEP，MGF1(SHA-256)，label=None，标准 base64

POST /internal/auth/login
  body: {email, recaptchaToken, keyPairId, encryptedPassword}
  → 200 {"ok":true,"invitations":[]}
  → Set-Cookie: ACCESS_TOKEN / REFRESH_TOKEN / *_EXPIRES_AT / tf_auth_profile
```

- **CMN-111 `비정상적인 접근이 감지되었습니다`** = reCAPTCHA 分数过低（也用于 signup）。
  解决方案：换新的页面上下文重新 `grecaptcha.enterprise.execute(siteKey, {action:'login'})` 重试；
  实测新注册账号首次登录可能连续 403，重试 1 次后即成功。
- `action` 必须是 `login`（signup 用 `signup`）。
- 有效 site key：`6Ld6CAEuAAAAABMcfRzFiXfSsiPjCgehCytp1laq`

### 2. 必要的租户头（最关键的坑）

TokenFactory 网关要求每个 API 请求带两个自定义头，缺失即 `400 PBG-019 테넌트 헤더 누락`：

```
tf-tenant-id: <tenantId>
tf-space-id:  <workspaceId>
```

来源：`tf_auth_profile` cookie（URL-encoded JSON）里的
`defaultTenantId` / `defaultWorkspaceId`。

注意：不是 `X-Tenant-Id`、`X-Workspace-Id`、`tenant-id` —— 这些全部无效。

### 3. 对话协议（三段式）

```
POST /api/v1/chat/turns
  headers: Cookie + tf-tenant-id + tf-space-id
  body: {"content":"用户输入",
         "settings":{...},                       ← 采样参数只能放顶层
         "models":[{"catalogModelId":"openai/gpt-6-astra","stream":true,
                    "systemPrompt":"..."}]}      ← systemPrompt 只能放 model 条目内
  → 201
    {"conversation":{"id":"<conv>"},
     "turn":{"id":"<turn>",
             "generations":[{"id":"<gen>","catalogModelId":"openai/gpt-6-astra",
                             "status":"pending"}]}}

POST /api/v1/chat/conversations/{conv}/turns/{turn}/generations/{gen}/completions
  headers: Accept: text/event-stream
  body: {}
  → 200 text/event-stream（上游本身就是 OpenAI chunk 格式）
```

**参数放置规则（2026-11-13 实测，放错会静默降级为错误对象）**：

| 字段 | 正确位置 | 放错的后果 |
|---|---|---|
| `settings`（`maxTokens` / `temperature` / `topP` / `response_format` / `reasoning_effort`） | **turn 顶层** | 放进 `models[]` 时 completions 返回 `application/json` 的 generation 对象，`errorMessage="모델 응답 중 오류가 발생했습니다."`，不走 SSE |
| `systemPrompt` | **`models[]` 条目内** | 放顶层会被忽略（模型不会遵守） |
| `historyMaxTurns` | turn 顶层 | — |

上游 SSE 实际内容：

```
data:{"id":"chatcmpl-...","object":"chat.completion.chunk","model":"gpt-6-astra-2026-09-03",
      "choices":[{"index":0,"delta":{"refusal":null,"role":"assistant","content":""},"finish_reason":null}]}
data:{"choices":[{"index":0,"delta":{"content":"Hi! How can I help you today?"},"finish_reason":null}]}
data:{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data:{"choices":[],"usage":{"completion_tokens":12,"prompt_tokens":267,...}}
data:{"object":"chat.completion.metadata","metadata":{"usage":{"totalTokens":"279"},
      "performance":{"durationMs":"2629"}}}
data:[DONE]
```

- 模型实际版本：`gpt-6-astra-2026-09-03`
- 每次对话消耗配额（FREE_MEMBER 每日 10 次）
- 追加对话用 `POST /api/v1/chat/conversations/{conv}/turns`，body `{"content":"..."}`

### 4. 模型目录

```
GET /api/v1/chat/public-models   → 73 个模型，无需登录
GET /api/v1/chat/model-groups    → 分组推荐
GET /api/v1/chat/quota           → 配额（需登录 + 租户头）
```

## 注册机要点（已固化）

- **纯协议可用**：signup 的 POST 由 curl 完成即可，不需要页面内 fetch。
- 必须带 `termConsents`，且 `versionId` 不等于 `termId`：

```json
[{"termId":1,"versionId":1},{"termId":4,"versionId":4},
 {"termId":5,"versionId":5},{"termId":8,"versionId":8}]
```

- signup 成功返回 **201**（不是 200）。
- 随后 `POST /api/v1/account/email-verifications/confirm` 用 6 位邮箱验证码确认，返回 `{"accountStatus":"active"}`。
- CMN-111 时换新 token + 新浏览器上下文重试，实测第 1~2 次即成功。

## 已验证的完整命令

```powershell
# 1. 注册新号
cd C:\Users\39455\workspace\tokenfactory\tools
python browser_signup.py 1

# 2. 协议登录并导出 cookies
cd C:\Users\39455\workspace\tokenfactory_proxy
python scripts\login_protocol.py --email <邮箱> --password <密码> --out data\cookies.json

# 3. 启动反代机
python -m tfproxy -c config.yaml

# 4. 调用 gpt-6-astra
curl http://127.0.0.1:8000/v1/chat/completions ^
  -H "Authorization: Bearer sk-tf-local-1" ^
  -H "Content-Type: application/json" ^
  -d '{"model":"gpt-6-astra","stream":true,"messages":[{"role":"user","content":"hello"}]}'
```

## 2026-11-13 修正记录（重要）

### A. 会话复用的致命坑

- `ACCESS_TOKEN` 只有约 **600s** 寿命，`REFRESH_TOKEN` 约 **14 天**。
- `POST /internal/auth/refresh` **只在 ACCESS_TOKEN 尚未过期时可用**；过期后一律
  `401 PBG-016 세션 만료`（或 `PBG-005 폐기된 인증 토큰`）。
- 所以账号池里缓存的 cookie **必须校验 `ACCESS_TOKEN_EXPIRES_AT`**，不能只看
  refresh token —— 否则会把几小时前的旧会话当有效返回，上游 `chat/turns` 全部 401。
- 正确顺序：有效 access → 纯协议 refresh → 账号池热号（10 分钟内登录过）→
  浏览器完整重登该账号。

### B. 配额是按账号计

- `GET /api/v1/chat/quota`（需登录 + 租户头）返回 `{limit:10, used:N}`。
- 不同账号 `used` 互不影响；同一个号用满 10 次后 completions 返回
  `429 CH-208`（**建 turn 仍返回 201**，配额错误发生在 completions 阶段）。
- 因此换号逻辑必须同时处理「建 turn 阶段」和「completions 阶段」的 429。
- 建议每次使用前用 quota 接口校正本地计数（免费，不消耗配额）。

### C. 验证码邮件重发接口

```
POST /api/v1/account/email-verifications/resend
  body: {"signupRequestId":"<id>"}
  → 200 {"sent":true,"retryAfterSeconds":10}
```

`/api/v1/account/email-verifications`（POST）只会 500 `CMN-501`，别用错。

### D. 浏览器复用

- 每个线程复用一台常驻 Chrome：首个 token 约 10s，之后 **0.4s**；
  完整登录从 20s 降到 **3.4s**（实测两次连续登录均 200）。
- reCAPTCHA token 在 2 分钟窗口内可重复用于登录，实测连续两次登录都成功。

### E. 工具调用（Claude Code 等客户端）

上游对 `gpt-6-astra` **不支持原生 function tools**（`CH-322`），因此代理层用
prompt 级 JSON 契约：`settings={"response_format":{"type":"json_object"},
"reasoning_effort":"low"}` → 模型输出
`{"type":"tool_calls","calls":[{"name":...,"arguments":{...}}]}` 或
`{"type":"final","content":"..."}`。

实测通过：OpenAI 非流式工具调用、OpenAI 流式 `tool_calls` 增量、
Anthropic `/v1/messages` 的 `tool_use` 块、以及工具结果回传后的最终回答。

### F. 并发写账号文件

注册进程与代理每 5 分钟都会写 `data/accounts.json`。两侧现在共用
`accounts.json.lock` + 临时文件 `os.replace` 原子替换，避免互相覆盖丢号。

## 待办（可继续优化）

- 上游验证邮件曾整体停发（`sent:true` 但 13 小时无邮件），注册侧已加
  `mail_blocked` 长冷却（30 分钟）与 resend 兜底；恢复后自动继续。
- 账号池预热：当前账号剩余 ≤ 2 次时后台预热下一个号，换号基本无感。
