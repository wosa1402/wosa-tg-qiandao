# TG Signer Web 快速开始（本地源码 / 容器平台）

本仓库已内置 Web 模块（FastAPI + 服务端渲染 + 子进程 worker + SSE 日志流），用于替代“纯终端操作”。  
**不依赖 PyPI 上的 `tg-signer[web]`**，建议直接在本仓库内运行或自行构建镜像。

> 注意：Telegram 的 `session`、`session_string` 属于敏感凭据，请勿写入仓库/镜像；建议开启 WebDAV 备份加密。

## 1. 本地运行（源码内启动）

在仓库根目录执行：

```bash
python -m tg_signer.webapp
```

默认数据目录：`./.tg-signer-web`  
如需自定义可使用环境变量 `TG_SIGNER_DATA_DIR`（可选）。

## 2. 首次登录（无 setup 模式）

- 系统会在首次启动时自动生成管理员密码并写入配置文件：  
  `./.tg-signer-web/web.config.json`
- 也会在启动日志中打印一次随机密码（账号固定为 `admin`）。

访问 `/login`，用 `admin + 随机密码` 登录。

## 3. Web 端配置入口

登录后进入 `/settings`，可配置：
- Telegram `API ID / API HASH`（可选，但建议填写）
- 代理 `TG_PROXY`
- 管理员密码
- WebDAV 备份（强烈建议）

配置会写入：`./.tg-signer-web/web.config.json`  
WebDAV 备份会包含：账号/任务/运行记录/配置（含 `web.config.json`）。

接着进入 `/tasks`：
- 先创建任务，再点“向导生成”用表单生成 `config.json`（也可随时切回“编辑配置”手写 JSON）。

## 4. 容器平台（Docker）

构建镜像：

```bash
docker build -f docker/Web.Dockerfile -t tg-signer-web:latest .
```

运行示例（仅演示端口映射，实际平台会注入 `PORT` 并提供 HTTPS 域名）：

```bash
docker run --rm -p 8000:8000 tg-signer-web:latest
```

无持久化卷的平台请务必在 `/settings` 中配置 WebDAV 备份，避免容器重启导致数据丢失。
