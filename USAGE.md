# 使用手册

## 一、注册机

### 一条命令全自动（推荐）

```powershell
cd C:\Users\39455\workspace\tokenfactory_proxy
python scripts\register_and_import.py --count 1
```

它依次完成：建邮箱 → 取 reCAPTCHA token → signup → 收验证码 → confirm → 登录 → **自动写入 `data/cookies.json` 和 `data/accounts.json`**。

常用参数：

| 参数 | 说明 |
|---|---|
| `--count N` | 注册 N 个号 |
| `--cookies-out PATH` | 会话 cookie 输出（默认 `data/cookies.json`） |
| `--accounts-out PATH` | 账号池输出（默认 `data/accounts.json`） |
| `--no-import` | 只注册不导入 |
| `--headed` | 浏览器显示窗口（默认无头） |

### 旧版注册机（仍可用）

```powershell
cd C:\Users\39455\workspace\tokenfactory\tools
python browser_signup.py 1        # 1 个号
python browser_signup.py 10       # 10 个号
```
结果写到 `tools/results/browser_register_*.json`（**不自动导入反代机**，需手动跑登录）。

## 二、反代机

### 启动

```powershell
cd C:\Users\39455\workspace\tokenfactory_proxy
python -m tfproxy -c config.yaml
```

或 `python run.py -c config.yaml`。默认监听 `127.0.0.1:8000`。

### 调用

```powershell
# 模型列表
curl http://127.0.0.1:8000/v1/models -H "Authorization: Bearer sk-tf-local-1"

# 非流式
curl http://127.0.0.1:8000/v1/chat/completions ^
  -H "Authorization: Bearer sk-tf-local-1" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"gpt-6-astra\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}"

# 流式
curl -N http://127.0.0.1:8000/v1/chat/completions ^
  -H "Authorization: Bearer sk-tf-local-1" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"gpt-6-astra\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}"
```

OpenAI SDK：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-tf-local-1")
r = client.chat.completions.create(model="gpt-6-astra",
        messages=[{"role":"user","content":"hi"}])
print(r.choices[0].message.content)
```

### 运维端点

| 端点 | 作用 |
|---|---|
| `GET /health` | 状态 + **token 真实剩余秒数** |
| `GET /admin/status` | 详细状态（需 `Bearer adm-local-1`） |
| `POST /admin/refresh-token` | 手动触发纯协议续期 |
| `POST /admin/reload-session` | 重新读取 cookie 文件 |
| `POST /admin/refresh-models` | 刷新模型目录 |
| `GET /v1/quota` | 上游配额 |

## 三、你现在问的三个问题（实测回答）

### Q1：注册完是自动导入反代机的吗？

**`register_and_import.py` 是自动的；`browser_signup.py` 不是。**

- 自动：脚本末尾直接调 `import_to_proxy()`，写 `data/cookies.json` 与 `data/accounts.json`。
  实测输出 `imported -> ...\data\cookies.json`。
- 反代机启动时，`AccountPool._load_registered_accounts()` 会读 `data/accounts.json`，
  所以账号池里能直接看到新号（`/health` 的 `accounts` 字段）。

### Q2：reCAPTCHA 过期了会自动起浏览器吗？

**会，但只在必要时。分层策略：**

| 层级 | 触发条件 | 需要浏览器 |
|---|---|---|
| 1 | ACCESS_TOKEN 仍有效 | ❌ |
| 2 | ACCESS_TOKEN 剩余 < 300s → `POST /internal/auth/refresh` | ❌ |
| 3 | ACCESS_TOKEN 已过期 → 用 `accounts.json` 的邮箱密码重新登录（需新 reCAPTCHA token） | ✅ 只取 token |

### Q3：没过期时是纯协议吗？

**是。** 实测证据（2026-11-12 23:38）：

```
[5] access_expires_in=311.4      ← 进入续期窗口
[6] access_expires_in=583        ← 自动续期成功

日志：
INFO [tfproxy.upstream.http] access token refreshed (now valid for 599.7s)
INFO [tfproxy.server] proactive refresh ok, next window in ~300s
```

整个过程没有启动浏览器，续期后立刻调用 `gpt-6-astra` 返回「续期成功」。

## 五、工具调用（function calling）

上游 Azure OpenAI 对 GPT 推理模型禁用了 `/v1/chat/completions` 上的原生函数工具
（实测错误：`Function tools with reasoning_effort are not supported for gpt-6-astra`），
因此工具调用在**代理层协议实现**，客户端完全无感：

### 工作原理

1. 请求带 `tools` 时，代理把函数清单注入一条 JSON 合同提示，并给上游加
   `settings: {"response_format": {"type":"json_object"}, "reasoning_effort":"low"}`——
   强制模型每轮只输出一个 JSON 对象（实测最可靠，模型无法用"我查不了"逃避格式）：
   ```json
   {"type":"tool_calls","calls":[{"name":"list_directory","arguments":{"path":"."}}]}
   {"type":"final","content":"项目结构如下..."}
   ```
2. 代理解析后转换成标准 OpenAI `tool_calls` / Anthropic `tool_use` 结构返回；
3. `tool` / `tool_result` 历史被序列化回纯文本，多轮工具循环完整可用；
4. 上游拒绝 `response_format` 时自动降级为 `<tool_calls>` XML 块协议；
   解析失败时自动带纠错提示重试一次。

### 实测记录（2026-11-13）

| 测试 | 结果 |
|---|---|
| OpenAI 非流式 tool_calls | ✅ `finish_reason:"tool_calls"`，参数正确 |
| tool 结果回填 → 最终回答 | ✅ 中文总结，`finish_reason:"stop"` |
| OpenAI 普通流式（无工具） | ✅ 不受影响 |
| Anthropic `/v1/messages` tool_use | ✅ `stop_reason:"tool_use"` |
| Anthropic 流式事件序列 | ✅ message_start → content_block(tool_use) → input_json_delta → message_stop |

### Claude Code 直连（Anthropic 协议）

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8000"
$env:ANTHROPIC_API_KEY  = "sk-tf-local-1"
claude
```

模型名随意（`claude-sonnet-4-5` 等），`models.aliases` 里的 `"*"` 会把它映射到
`openai/gpt-6-astra`（当前 config.yaml 已这样设置）。

### Cline / Roo Code / 任意 OpenAI 客户端

```yaml
base_url: http://127.0.0.1:8000/v1
api_key: sk-tf-local-1
model: gpt-6-astra
```

### 注意

- **配额**：每账号每天 10 次对话调用；Claude Code 一次编码会话很容易烧光。
  对策就是注册机——多攒账号进 `accounts.json` 轮换。
- 带 `tools` 的请求走缓冲路径（整段收完再解析），首字延迟略高于纯流式；
  无工具请求仍是真流式直通。

## 六、REFRESH_TOKEN 到期管理（每号独立计算）

| 配置 | 默认 | 说明 |
|---|---|---|
| `maintenance.renew_refresh_days` | 3 | 剩余 < 3 天 → 视为临期 |
| `maintenance.max_browser_concurrency` | 2 | **同时最多 2 个浏览器** |
| `maintenance.interval_seconds` | 1800 | 巡检间隔 |
| `maintenance.max_renewals_per_cycle` | 4 | 每轮最多续 4 个号，错峰 2s 启动 |

运作方式（已实测）：

1. 后台每 30 分钟巡检一次 `accounts.json` 里每个账号的 `REFRESH_TOKEN_EXPIRES_AT`；
2. 临期账号按"最早到期优先"排序，逐个起浏览器重登（并发闸门 = 2）；
3. 重登成功 → 新 cookie 写回 `accounts.json`；若正是当前活动账号，活动会话原地更新；
4. 续期后该号恢复 14 天有效期，自动退出临期名单。

实测日志：

```
maintenance: 2 account(s) due for browser renewal: [...]
browser renewal start: tfXX (browser slots 1/2 used)
browser renewal start: tfYY (browser slots 2/2 used)
browser renewal ok: tfXX (refresh valid ~14.0 days)
maintenance cycle result: {'tfXX': 'ok'}
```

手动触发：`POST /admin/maintenance`（Bearer adm-local-1）。
账号剩余天数在 `GET /admin/status` 的 `refresh_expires_in_days` 可查。

分层兜底不变：access token 过期前纯协议刷新 → 过期后账号池浏览器重登 → 巡检兜底。

## 七、配额计数与自动换号

上游每账号每天 10 次对话调用（`upstream.daily_quota_per_account` 可配）。代理自动处理：

- 每成功创建一次对话（上游 201）即计数，持久化在 `data/quota.json`（按 UTC+9 自然日重置）；
- 请求前检查当前账号余额，用完自动**纯协议**切到下一个有余额的账号；
- 上游直接返回 429/CH-208 时，标记该号当日耗尽并换号重试一次；
- 全部账号耗尽时报错提示注册新号。

`GET /admin/status` 的 `quota` 字段可查每个号今日用量。

## 八、批量注册（500 个目标）

```powershell
# 生产目录下运行驱动循环：自动分批（每批 50），直到 500 个
powershell -File scripts\registration_driver.ps1 -Target 500 -BatchSize 50
```

特性：
- 断点续跑：`--target 500` 自动跳过已有账号，中断后重跑即继续；
- 增量落盘：每注册成功一个立即写入 `accounts.json` 与 `results`，KeyboardInterrupt /
  关终端只丢当前正在注册的那一个，已完成账号不丢失；
- 任何 signup 失败（CMN-111 / 网关 403 / 限流）自动换 token 重试最多 4 次。

## 九、关键运维事实

- **ACCESS_TOKEN 寿命约 600 秒**（10 分钟）。
- **`/internal/auth/refresh` 只在 ACCESS_TOKEN 仍有效时可用**；
  一旦过期就返回 `401 PBG-005 폐기된 인증 토큰`，此时只能完整重新登录。
  所以续期必须提前，代码窗口设为剩余 < 300s。
- **REFRESH_TOKEN 寿命约 14 天**，期间可无限次续期。
- 每次完整登录需要新 reCAPTCHA token（一次性、约 2 分钟有效）。
- 新注册账号首次登录常吃 `CMN-111`，换新上下文重试 1~2 次即成功。
