# TG Signer Web API 合约草案（OpenAPI 风格）

> 说明：这是“用于实现前对齐”的接口设计草案，不是最终 OpenAPI YAML。后续实现阶段可以按本草案生成/维护 OpenAPI 文档与前端调用层。
>
> 现状：当前实现已改为单层管理员登录（`admin` + 密码），BasicAuth 相关内容仅作为历史草案参考。

## 1. 约定

### 1.1 基础信息

- Base URL：`https://<你的域名>`
- API 前缀：`/api`
- 数据格式：除日志流外均为 `application/json; charset=utf-8`

### 1.2 鉴权（两层）

1) BasicAuth（外层）
- 若有反向代理：由反向代理承担（推荐）
- 若平台 Ingress 不提供访问保护：由应用内 BasicAuth 中间件承担
- 所有路径默认需要 BasicAuth（先挡扫描与爆破）

2) 应用内登录（内层）
- 登录成功后写入安全 Cookie 会话（`HttpOnly`、`Secure`、`SameSite`）
- 写操作接口需要 CSRF 防护（表单或 Header token）

> 目的：BasicAuth 负责挡扫描/爆破；应用内登录负责细粒度授权与“敏感操作二次确认”。

### 1.3 统一错误结构

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "字段校验失败",
    "details": {
      "fields": {
        "task_name": "只允许字母/数字/下划线/中划线"
      }
    }
  }
}
```

常见 `error.code`：
- `UNAUTHORIZED`：未登录/会话过期
- `FORBIDDEN`：无权限（单用户场景一般不会出现）
- `NOT_FOUND`：资源不存在
- `CONFLICT`：资源冲突（如账号正在运行，无法重复启动）
- `VALIDATION_ERROR`：字段校验失败
- `INTERNAL_ERROR`：服务端异常（需配合 run_id/log 排障）

---

## 2. Auth（应用内登录）

### POST `/api/auth/login`

用途：应用内登录（内层）。

请求：
```json
{ "password": "******" }
```

响应：
```json
{ "ok": true }
```

备注：
- 通过 Set-Cookie 写入会话
- 建议对该接口做限速与失败冷却

### POST `/api/auth/logout`

响应：
```json
{ "ok": true }
```

### GET `/api/auth/me`

响应：
```json
{ "logged_in": true }
```

---

## 3. Accounts（Telegram 账号）

### 3.1 数据模型（简化）

```json
{
  "account_name": "my_account",
  "status": "logged_out",
  "last_login_at": "2025-01-01T00:00:00Z",
  "last_error": null
}
```

`status`：`logged_out | logging_in | logged_in | error`

### GET `/api/accounts`

响应：
```json
{ "items": [ { "account_name": "a", "status": "logged_in" } ] }
```

### POST `/api/accounts`

用途：创建账号记录（不触发登录）。

请求：
```json
{ "account_name": "account_a" }
```

响应：
```json
{ "account_name": "account_a", "status": "logged_out" }
```

### POST `/api/accounts/{account}/login/start`

用途：开始登录流程，触发发送验证码（短信/Telegram 端）。

请求：
```json
{ "phone_number": "+8613800138000" }
```

响应：
```json
{ "login_id": "uuid", "next": "verify_code" }
```

约束：
- `login_id` 只在短时间内有效（如 10 分钟）
- 服务端保存 `phone_code_hash` 等临时状态，绝不写日志、绝不落盘（或只写入受控 DB 且不含明文敏感字段）

### POST `/api/accounts/{account}/login/verify`

用途：提交验证码。

请求：
```json
{ "login_id": "uuid", "code": "12345" }
```

响应（不需要二次密码）：
```json
{ "ok": true, "status": "logged_in" }
```

响应（需要二次密码）：
```json
{ "ok": false, "login_id": "uuid", "next": "password", "hint": "two-factor enabled" }
```

### POST `/api/accounts/{account}/login/password`

用途：提交二次密码（如启用了 2FA）。

请求：
```json
{ "login_id": "uuid", "password": "******" }
```

响应：
```json
{ "ok": true, "status": "logged_in" }
```

### POST `/api/accounts/{account}/logout`

用途：登出并删除 session 文件。

响应：
```json
{ "ok": true }
```

---

## 4. Tasks（任务/配置）

### 4.1 数据模型（简化）

```json
{
  "task_name": "my_sign",
  "type": "signer",
  "account_name": "account_a",
  "enabled": false,
  "updated_at": "2025-01-01T00:00:00Z"
}
```

### GET `/api/tasks`

响应：
```json
{ "items": [ { "task_name": "my_sign", "type": "signer", "account_name": "a" } ] }
```

### POST `/api/tasks`

用途：创建任务（可选择空模板或从导入内容创建）。

请求（最小）：
```json
{ "task_name": "my_sign", "type": "signer", "account_name": "account_a" }
```

响应：
```json
{ "task_name": "my_sign" }
```

### GET `/api/tasks/{task}`

响应（含 config）：
```json
{
  "task_name": "my_sign",
  "type": "signer",
  "account_name": "account_a",
  "enabled": false,
  "config": { "sign_at": "0 6 * * *", "random_seconds": 0, "chats": [] }
}
```

### PUT `/api/tasks/{task}`

用途：保存任务元数据与配置；服务端负责 Pydantic 校验并返回字段级错误。

请求：
```json
{
  "enabled": true,
  "account_name": "account_a",
  "config": { "sign_at": "0 6 * * *", "random_seconds": 0, "chats": [] }
}
```

响应：
```json
{ "ok": true }
```

### POST `/api/tasks/{task}/export`

响应：
```json
{ "format": "json", "content": "{...}" }
```

### POST `/api/tasks/{task}/import`

请求：
```json
{ "format": "json", "content": "{...}" }
```

响应：
```json
{ "ok": true }
```

---

## 5. Runs（运行实例）

### 5.1 数据模型（简化）

```json
{
  "run_id": "uuid",
  "task_name": "my_sign",
  "account_name": "account_a",
  "mode": "run",
  "status": "running",
  "started_at": "2025-01-01T00:00:00Z",
  "finished_at": null,
  "error_message": null
}
```

`mode`：`run | run_once | monitor`

`status`：`queued | running | success | failed | stopped`

### POST `/api/tasks/{task}/run`

请求（可选覆盖参数）：
```json
{ "num_of_dialogs": 50 }
```

响应：
```json
{ "run_id": "uuid" }
```

错误：
- `CONFLICT`：同一账号已有运行中的 worker（session 冲突）

### POST `/api/tasks/{task}/run-once`

同上。

### POST `/api/runs/{run_id}/stop`

响应：
```json
{ "ok": true }
```

### GET `/api/runs`

查询参数（示例）：
- `task_name`
- `account_name`
- `status`
- `since` / `until`

响应：
```json
{ "items": [ { "run_id": "uuid", "status": "success" } ] }
```

### GET `/api/runs/{run_id}`

响应：
```json
{ "run": { "run_id": "uuid", "status": "failed", "error_message": "..." } }
```

---

## 6. Logs（日志）

### GET `/api/runs/{run_id}/logs`

用途：拉取日志片段（用于分页/回放）。

查询参数（示例）：
- `offset`：字节偏移（或行号）
- `limit`：最大字节数（或行数）

响应：
```json
{ "offset": 0, "content": "..." }
```

### GET `/api/runs/{run_id}/logs/stream`

用途：实时日志流（推荐 SSE，代理兼容性更好）。

- 响应类型：`text/event-stream`
- 事件示例：
  - `event: log` + `data: ...`
  - `event: status` + `data: {"status":"success"}`

---

## 7. Worker 协议（供子进程执行）

> 这里定义“Web 进程如何拉起 worker”需要的最小协议，保证后续实现可测、可替换。

建议的 worker 入口形态（示例）：

- `python -m tg_signer.worker --run-id <uuid> --task <task_name> --account <account_name> --workdir /data/workdir --session-dir /data/sessions --mode run`

关键要求：
- worker 必须把日志写到 Web 指定的 `log_path`
- worker 必须以 exit code 表示成功/失败/被停止
- Web 必须能基于 `run_id` 关联到运行记录与日志

---

## 8. Backup（WebDAV 备份/恢复，可选但在“无持久化卷”平台上建议必做）

> 场景：平台没有持久化卷，只能靠外部存储保留 `/sessions`、`/workdir` 等关键数据。
> 这些接口用于可观测与手动触发，自动备份仍建议由服务端定时器完成。

### GET `/api/backup/status`

响应（示例）：
```json
{
  "enabled": true,
  "remote_path": "/tg-signer/backup.tar.gz",
  "last_pull_at": "2025-01-01T00:00:00Z",
  "last_push_at": "2025-01-01T00:05:00Z",
  "last_error": null
}
```

### POST `/api/backup/pull`

用途：从 WebDAV 拉取备份并恢复到本地（高风险操作）。

请求：
```json
{ "confirm": true }
```

响应：
```json
{ "ok": true }
```

约束建议：
- 若存在运行中的 `run`：拒绝执行（避免覆盖正在使用的 session/workdir）
- 恢复完成后建议要求重启 worker/刷新内存缓存

### POST `/api/backup/push`

用途：立刻把当前 `/data` 关键目录打包并上传到 WebDAV。

请求：
```json
{ "confirm": true }
```

响应：
```json
{ "ok": true }
```
