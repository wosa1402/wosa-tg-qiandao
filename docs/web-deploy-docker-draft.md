# TG Signer Web 部署草案（容器平台/Docker + 域名 + 单层登录 + 子进程 worker）

> 目标：给出可落地的部署形态与约束，让后续实现/上线“不踩坑”。本文件是设计草案，不包含可直接运行的最终配置。
>
> 现状：当前实现已改为单层管理员登录（`admin` + 密码），BasicAuth 相关内容仅作为历史草案参考。

## 1. 容器平台部署（平台送域名/绑定端口，推荐）

你描述的环境更像“PaaS 容器平台”：部署一个镜像后，平台会给你一个域名，并把外部流量转发到容器暴露的端口。
这种情况下，平台本身就充当了反向代理/Ingress（通常也负责 HTTPS），因此大多数时候不需要你再额外跑一个 `proxy` 容器。

### 1.1 推荐拓扑（两层鉴权仍保留）

- 平台 Ingress（平台自带）
  - 负责域名绑定与转发（你已确认自带 HTTPS/TLS）
- `app`（你部署的单容器）
  - FastAPI：页面 + API + 日志流
  - WorkerManager：按需拉起 worker 子进程（每个 run 一个进程）
  - 内层鉴权：应用内登录（密码 + 安全 Cookie）+ CSRF 防护

你已确认平台不提供白名单/访问保护，因此建议把“外层”直接落在 `app`：

- 外层：应用内 BasicAuth 中间件（保护所有页面与 API）
- 内层：应用内登录（保护敏感操作 + 形成业务会话）

### 1.2 端口与启动命令（容器平台常见约束）

- 必须监听 `0.0.0.0`（不能只绑 `127.0.0.1`）。
- 端口通常由平台注入环境变量 `PORT`（或要求你在平台设置一个固定端口）。

建议启动方式（示例）：

- `uvicorn ... --host 0.0.0.0 --port ${PORT} --workers 1`

> 先坚持 `--workers 1`：因为我们要在同一容器内拉起 worker 子进程，多 worker 会引入并发竞态与重复调度的问题；后续扩展到多实例/多 worker 需要引入分布式锁/队列再谈。

补充：你已确认平台允许子进程与信号（SIGINT/SIGTERM），因此“子进程 worker”方案可行。

### 1.3 持久化存储（必须确认）

你希望在 Web 中直接登录 Telegram，这意味着 **session 必须持久化**。

理想方案：

- 平台提供“持久化卷/磁盘”，并挂载到 `/data`（至少包含 `/data/sessions` 与 `/data/workdir`）。

你的现状（平台不支持持久化卷）：

- 容器文件系统是临时的：容器重启会丢 session、配置、运行历史。
- 你提到可用 WebDAV 备份/拉取：这可以作为“外置持久化层”，但要接受“最终一致 + 可能丢失最近几分钟数据”的现实。

#### 1.3.1 推荐的 WebDAV 同步策略（单实例）

把 `/data` 视为“本地缓存”，把 WebDAV 视为“持久化源”：

- 启动时：从 WebDAV 拉取最近一次备份 → 解包覆盖到 `/data` → 再启动 Web 服务。
- 运行中：以下事件触发“增量或全量上传备份”：
  - 账号登录成功/登出（session 变化）
  - 任务配置保存/导入（workdir 变化）
  - run 状态落库（runs 变化，可选）
  - 定时（例如每 1~5 分钟一次，兜底）
- 退出/重启前：尽力执行一次备份上传（不能保证平台一定给优雅退出时间，所以仍要靠定时兜底）。

#### 1.3.2 备份内容建议

建议分两类：

- **必须备份**：`/data/sessions`、`/data/workdir`、`/data/state`（若使用 SQLite）。
- **可选备份**：`/data/logs/runs`（通常很大，建议只保留最近 N 次或仅保留失败 run）。

#### 1.3.3 冲突与一致性（避免“备份覆盖备份”）

单实例部署时冲突概率最低，但仍建议做最小保护：

- 每次上传前读取远端 `ETag`/`Last-Modified`（如果 WebDAV 支持）。
- 如果远端在你上次同步后发生变化：
  - 方案 A（保守）：拒绝覆盖并报警，要求人工处理（推荐）
  - 方案 B（简单）：最后写入覆盖（不推荐，容易把有效状态覆盖没）

#### 1.3.4 安全建议（WebDAV 存的都是敏感资产）

- WebDAV 必须走 HTTPS。
- WebDAV 凭证只通过环境变量注入（不要写进镜像/仓库）。
- 强烈建议对备份包做加密（即使 WebDAV 被入侵也不至于直接拿到 Telegram session）：
  - 备份加密密钥通过环境变量注入（例如 `TG_SIGNER_BACKUP_ENCRYPTION_KEY`）。

### 1.4 副本与扩缩容（强烈建议先单实例）

- 单用户 + Telegram session + 文件型 workdir 的组合，最稳妥是 **只跑 1 个实例**。
- 如果平台会自动扩容/多副本：需要额外做分布式锁（至少按 `account_name` 加锁）和共享存储，否则会出现重复运行与会话冲突。

### 1.5 SSE/WebSocket 支持

- 日志流推荐 SSE（`text/event-stream`），对反代/平台的兼容性一般比 WebSocket 更好。
- 仍建议确认平台对长连接的超时策略（比如 60s/120s idle timeout），必要时在前端做自动重连。

## 2. 自管服务器部署（docker compose 两容器，备选）

当你希望完全掌控 TLS、证书、限速策略，或平台支持多容器编排时，可以采用“两容器”：

- `proxy`：Caddy 或 Nginx（建议 Caddy 起步）
  - 负责 HTTPS（TLS 证书）
  - 负责 BasicAuth（外层鉴权）
  - 反向代理到 `app`
- `app`：tg-signer Web 服务（FastAPI）
  - 页面 + API + 日志流
  - WorkerManager：按需拉起 worker 子进程（每个 run 一个进程）

## 3. 数据卷与目录规划（建议）

建议统一挂载到 `/data`：

```
/data
  /workdir        # tg-signer 的工作目录（原 --workdir），任务配置/记录
  /sessions       # Telegram session 存储目录（原 --session_dir）
  /logs
    /app          # Web 服务日志
    /runs         # 按 run_id 分流的任务日志
  /state
    state.db      # SQLite（建议），存 accounts/tasks/runs 等元数据
```

> 重点：`/sessions` 与 `/workdir` 必须持久化，否则容器重建会丢登录状态与配置。

## 4. 关键环境变量（建议）

### 4.1 Telegram

- `TG_API_ID`：你自己的 Telegram App ID
- `TG_API_HASH`：你自己的 Telegram App HASH
- `TG_PROXY`（可选）：如需代理，例如 `socks5://127.0.0.1:1080`

### 4.2 Web 应用内登录（内层鉴权）

建议用“单用户管理员密码”，通过环境变量注入（避免写入镜像与仓库）：

- `TG_SIGNER_WEB_ADMIN_PASSWORD`：管理员密码（或使用 hash 形式）
- `TG_SIGNER_WEB_SESSION_SECRET`：Cookie 会话密钥（随机长字符串）

> 约束：密码与会话密钥不要写进 git，不要打印到日志。

### 4.3 外层 BasicAuth（当平台无白名单/访问保护时必须启用）

- `TG_SIGNER_WEB_BASIC_AUTH_USERNAME`
- `TG_SIGNER_WEB_BASIC_AUTH_PASSWORD`（或 `..._HASH` 形式，避免明文；实现阶段再定）

### 4.4 WebDAV 备份（当平台无持久化卷时启用）

- `TG_SIGNER_BACKUP_WEBDAV_URL`：WebDAV 端点（建议 HTTPS）
- `TG_SIGNER_BACKUP_WEBDAV_USERNAME` / `TG_SIGNER_BACKUP_WEBDAV_PASSWORD`：WebDAV 凭证
- `TG_SIGNER_BACKUP_REMOTE_PATH`：远端路径
  - 可以是“目录”（以 `/` 结尾），例如 `/tg-signer/`
  - 也可以是“完整文件路径”，例如 `/tg-signer/backup.latest.tar.gz`
  - 约定：若填的是目录，则默认文件名使用 `backup.latest.tar.gz`
- `TG_SIGNER_BACKUP_INTERVAL_SECONDS`：定时备份间隔（例如 `300`）
- `TG_SIGNER_BACKUP_ENCRYPTION_KEY`：备份加密密钥（可选但强烈建议）

### 4.5 容器平台环境变量清单（建议直接照此配置）

> 说明：以下示例全部是“占位符”，不要把明文密码写进仓库；在平台的环境变量/密钥管理里配置即可。

必填（建议）：

- `TG_API_ID`：你的 Telegram App ID
- `TG_API_HASH`：你的 Telegram App HASH
- `TG_SIGNER_WEB_BASIC_AUTH_USERNAME`：外层 BasicAuth 用户名（例如 `admin`）
- `TG_SIGNER_WEB_BASIC_AUTH_PASSWORD`：外层 BasicAuth 密码（强随机）
- `TG_SIGNER_WEB_ADMIN_PASSWORD`：内层应用登录密码（强随机，可与 BasicAuth 不同）
- `TG_SIGNER_WEB_SESSION_SECRET`：会话密钥（强随机长字符串）
- `TG_SIGNER_BACKUP_WEBDAV_URL`：WebDAV URL（HTTPS）
- `TG_SIGNER_BACKUP_WEBDAV_USERNAME`
- `TG_SIGNER_BACKUP_WEBDAV_PASSWORD`
- `TG_SIGNER_BACKUP_REMOTE_PATH`：例如 `/tg-signer/`
- `TG_SIGNER_BACKUP_INTERVAL_SECONDS`：例如 `300`
- `TG_SIGNER_BACKUP_ENCRYPTION_KEY`：备份加密密钥（强随机；建议 32+ 字节）

可选：

- `TG_PROXY`：如需代理（`socks5://...`）

平台常见内置变量（通常由平台注入，无需你手动设置）：

- `PORT`：应用监听端口（你的镜像启动命令需要读取它并监听 `0.0.0.0:$PORT`）

## 5. docker-compose 结构（草案）

下面是结构示例（参数与镜像名后续实现阶段再定）：

```yaml
services:
  app:
    image: tg-signer:latest
    volumes:
      - ./data:/data
    environment:
      - TG_API_ID=*****
      - TG_API_HASH=*****
      - TG_SIGNER_WEB_ADMIN_PASSWORD=*****
      - TG_SIGNER_WEB_SESSION_SECRET=*****
    expose:
      - "8000"
    command: >
      uvicorn tg_signer.webapp.app:app
      --host 0.0.0.0
      --port 8000
      --workers 1

  proxy:
    image: caddy:2
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - app

volumes:
  caddy_data:
  caddy_config:
```

关键点：
- `app` 只 `expose` 给内网，不 `ports` 映射到宿主机（避免绕过 BasicAuth）。
- `--workers 1`：先从单 worker 起步，减少竞态；未来扩展再加锁/队列。
- 如果 worker 作为子进程运行，建议 `docker run --init` 或镜像内置 `tini`（防僵尸进程）。

## 6. Caddyfile（草案）

```caddyfile
your.domain.com {
  encode gzip

  basicauth /* {
    admin {$BASIC_AUTH_HASH}
  }

  reverse_proxy app:8000
}
```

说明：
- `{$BASIC_AUTH_HASH}` 使用 Caddy 的 hash 生成（不要放明文）。
- 如果你更熟 Nginx，也可以用 Nginx 做同样的 TLS + BasicAuth + 反代。

## 7. 上线安全清单（强烈建议）

- BasicAuth + 应用内登录都启用（你已选择两层方案）。
- 若平台不提供白名单/访问保护：外层 BasicAuth 必须由应用内中间件承担，且需要限速/失败冷却。
- 关闭 `app` 的对外端口暴露，只允许 `proxy`/平台 Ingress 对外。
- 运行日志与配置导出入口增加二次确认（避免误触泄露）。
- 对登录与敏感接口做限速（proxy 或 app 任一层都可以）。
- 无持久化卷时：确保 WebDAV 备份链路可靠（尤其 `/sessions`、`/workdir`、`/state`）。
