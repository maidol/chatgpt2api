# 部署与升级

状态：当前

本项目的发布镜像默认是 `ghcr.io/yukkcat/chatgpt2api:latest`。标准 Compose 将服务暴露在 `3000` 端口，使用 `chatgpt2api-runtime` 命名卷保存可更新的应用运行目录，并单独挂载本地 `data/` 和 `config.json`。运行时配置和数据不应提交到 Git。

## Docker 部署

```bash
git clone https://github.com/yukkcat/chatgpt2api.git
cd chatgpt2api
cp .env.example .env
# 将 .env 中的 CHATGPT2API_AUTH_KEY=your_secret_key_here 替换为私有密钥。
test -f config.json || printf '{}\n' > config.json
docker compose up -d
```

镜像中的 `/opt/chatgpt2api` 是只读应用种子，`/app` 是受管运行目录。首次启动或镜像版本变化时，入口脚本会用镜像种子刷新 `/app`，再按锁文件同步 Python 依赖；同一镜像正常重启时会保留控制台在线更新后的运行版本。业务数据始终留在独立的 `data/` 挂载中。

### 本地 PostgreSQL 18

需要由 Compose 一并运行 PostgreSQL 时，在 `.env` 中设置数据库密码：

```dotenv
POSTGRES_PASSWORD=replace_with_a_strong_password
```

然后同时加载基础 Compose 和 PostgreSQL overlay：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

`docker-compose.postgres.yml` 使用官方 `postgres:18-alpine` 镜像，等待数据库健康后再启动应用，并将数据持久化到 `chatgpt2api-postgres-data` 命名卷。数据库端口默认不暴露到宿主机。

`POSTGRES_PASSWORD` 会同时用于初始化数据库和构造 `DATABASE_URL`，因此请仅使用 URL 安全字符（字母、数字、下划线或连字符），不要在两个位置分别编码密码。
启用该模式后，后续启动、升级、查看状态和停止服务都应同时指定这两个 Compose 文件。

查看状态与日志：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml ps
docker compose -f docker-compose.yml -f docker-compose.postgres.yml logs -f postgres app
```

`.env` 中的 `CHATGPT2API_AUTH_KEY` 优先于 `config.json` 的 `auth-key`。若要使用 `config.json`，先删除或注释 `.env` 中的该值，再填写 `auth-key`。

默认地址：

- 控制台：`http://localhost:3000`
- API：`http://localhost:3000/v1`

也可以使用仓库安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/yukkcat/chatgpt2api/main/deploy/install.sh | sudo bash
```

安装脚本会明确询问 Application Database：

- SQLite 本地文件（默认）
- PostgreSQL 18 本地容器（Docker 模式）
- 已有 PostgreSQL URL

选择本地 PostgreSQL 时，脚本会自动生成并保存数据库密码，下载 `docker-compose.postgres.yml`，再与主 Compose 一起启动。重复运行安装脚本会复用已有密码。`CHATGPT2API_THREAD_TOKENS` 默认是 `120`，表示后端同步工作线程的并发容量，只要求正整数且不设置人为最高值；账号、代理和上游服务仍分别执行自己的并发限制。

## 本地开发

后端：

```bash
git clone https://github.com/yukkcat/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

Vue 控制台：

```bash
cd web-vue
npm install
npm run dev
```

前端开发服务器默认使用 Vite 端口；后端仍读取项目根目录的 `config.json` 和 `data/`。

## 存储边界

`DATABASE_URL` 选择 Application Database；未设置时使用
`data/chatgpt2api.db`。支持 SQLite 与 PostgreSQL 18，不再通过
`STORAGE_BACKEND` 选择 JSON、Git 或账号专用数据库。

选择数据库不是旧数据迁移操作，不会自动导入 JSON、JSONL、Git 或旧账号
SQLite 文件。图片文件及其相关索引仍按图片存储边界管理。完整边界见
[`storage-architecture.md`](storage-architecture.md)。

## 升级

升级前先在系统设置中执行一次 R2 备份，并确认状态为成功。备份归档始终包含
Application Database：SQLite 使用 `data/application-database.sqlite3`，PostgreSQL
使用 `data/application-database.pgdump`。图片任务记录、PPT / PSD 文件和图片目录
按备份设置选择；外部 WebDAV 仍需独立备份。

未配置 R2 时，应先停止服务再备份。SQLite 可以在停服后复制 `data/chatgpt2api.db`；
PostgreSQL 必须使用 `pg_dump --format=custom`，不能用 `tar data/` 代替数据库备份。
`config.json` 只保留 `auth-key` 等启动配置，也应单独保存：

```bash
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

本地 PostgreSQL Compose 可直接在数据库容器内导出：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T postgres \
  sh -c 'pg_dump --format=custom --no-owner --no-privileges \
  -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

### 控制台在线更新

使用当前标准 Compose 部署时，版本弹窗可直接启动在线更新。后端负责下载发布包、校验 SHA-256 与更新清单、替换运行文件、同步依赖和安排容器重启；前端只展示后端任务，刷新页面或重启完成后仍可恢复任务结果。文件或依赖同步失败时会恢复旧文件，并按旧锁文件重新同步依赖。

源码运行和没有挂载受管 `/app` 运行目录的旧容器不会显示“立即更新”，只会给出 Git 或镜像升级提示。首次切换到受管运行目录时，应先更新仓库中的 Compose 文件，再执行一次镜像升级：

```bash
git pull --ff-only
docker compose pull
docker compose up -d
```

不要把 `/app` 运行时卷当作业务备份；它可以从发布镜像重新创建。不要使用 `docker compose down -v` 执行普通升级，因为该命令还会删除 Compose 管理的命名卷。

### 本地 checkout 构建升级（仅默认 SQLite）

当 feature 分支尚未发布到 GHCR、但已经通过 `scp`、`git bundle` 或其他人工方式传到生产 Docker 主机时，可以在生产主机上从该干净 checkout 本地构建并升级现有的安装器管理部署：

```bash
sudo INSTALL_DIR=/opt/chatgpt2api \
  bash /path/to/chatgpt2api/deploy/build-and-upgrade.sh
```

先做离线预检（不构建、不停服务、不修改部署）：

```bash
sudo INSTALL_DIR=/opt/chatgpt2api \
  bash /path/to/chatgpt2api/deploy/build-and-upgrade.sh --dry-run
```

脚本要求源码 checkout 没有未提交改动、生产机使用标准 `docker-compose.yml`、默认 SQLite 文件 `data/chatgpt2api.db`，并且现有 `chatgpt2api` 容器正在运行。它拒绝 PostgreSQL、非默认 SQLite 路径和自定义数据库 URL；这些部署应使用各自的备份与升级流程。

脚本还要求运行中 `/app/VERSION`、当前镜像种子 `/opt/chatgpt2api/VERSION` 与源码 `VERSION` 三者一致。控制台在线更新过的部署（`/app` 版本高于镜像种子）会被拒绝：入口脚本重新同步 `/app` 时会丢弃在线更新的代码，回滚也只能恢复旧镜像种子而不是原先运行的代码。版本不一致时应走镜像发布升级流程。

脚本不会 fetch/checkout 源码，不会 push 或 pull 应用镜像，也不会执行 `docker compose down -v`。本地 Docker 构建使用当前完整 commit SHA 作为镜像标签，并复用已缓存的构建层；缓存未命中时，Docker 可能访问基础镜像仓库，构建步骤也可能访问 npm 与 Python 包索引。升级前会备份 SQLite（包括 WAL 模式下的 `chatgpt2api.db-wal` / `chatgpt2api.db-shm`）、`.env` 和 `config.json`，并把旧镜像回滚标签写入备份目录的 `ROLLBACK_IMAGE`；成功后保留备份和回滚标签。

运行前会打印 commit、安装目录、镜像、备份目录和健康检查地址，并要求确认；自动化调用可显式加 `--yes`：

```bash
sudo INSTALL_DIR=/opt/chatgpt2api \
  bash /path/to/chatgpt2api/deploy/build-and-upgrade.sh --yes
```

由于本分支没有修改 `VERSION`，脚本会清理受管运行卷中的 `.chatgpt2api-image-version`，让 entrypoint 从新镜像重新同步代码。构建失败不会停服务；重启或健康检查失败时会恢复备份文件和旧镜像。停服之后若收到 Ctrl-C、`TERM` 或 SSH 断开的 `HUP`，脚本同样执行回滚，回滚期间忽略再次中断；仍建议在 `tmux` / `screen` 中运行。脚本只覆盖应用程序、默认 SQLite 和启动配置，不备份外部 WebDAV 或其他独立图片存储。

升级完成后检查：

```bash
docker compose -f /opt/chatgpt2api/docker-compose.yml ps
docker logs --tail=200 chatgpt2api
```

脚本的手动回滚备份位于 `/opt/chatgpt2api/backups/chatgpt2api-upgrade-*`。若自动回滚也失败，脚本会打印完整的恢复命令；手动步骤为：

1. 停止 app：`docker compose -f docker-compose.yml stop app`。
2. 恢复备份目录中的 `chatgpt2api.db`；删除当前的 `chatgpt2api.db-wal` / `chatgpt2api.db-shm`，若备份中有这两个文件则一并恢复；再恢复 `.env` 和 `config.json`。
3. 将 `.env` 中的 `CHATGPT2API_IMAGE` 设为备份目录 `ROLLBACK_IMAGE` 文件记录的旧镜像标签。
4. 清除受管运行卷中的 `.chatgpt2api-image-version`，否则旧镜像会因 `VERSION` 相同而跳过重新同步、继续运行新代码：

   ```bash
   docker run --rm -v <运行卷名>:/app --entrypoint python <旧镜像标签> \
     -c 'from pathlib import Path; Path("/app/.chatgpt2api-image-version").unlink(missing_ok=True)'
   ```

   运行卷名可用 `docker volume ls | grep chatgpt2api-runtime` 查看。
5. 执行 `docker compose -f docker-compose.yml up -d --no-build --force-recreate app`。

### 命令行升级

镜像部署升级：

```bash
docker compose pull
docker compose up -d
```

本地 PostgreSQL Compose 部署升级：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml pull
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

镜像部署固定或回退版本时，在 `.env` 设置 `CHATGPT2API_IMAGE=ghcr.io/yukkcat/chatgpt2api:<tag>`，再执行对应的 `pull` 与 `up`。镜像版本变化后，入口脚本会用该镜像刷新受管运行目录；Git 检出标签只影响源码运行，不会改变 Compose 使用的镜像版本。升级后检查：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 回滚与维护

先停止对应 Compose，再恢复经过验证的代码 / 镜像和备份数据；不要在运行时直接覆盖 `data/`。常用命令：

```bash
docker compose restart
docker compose down
```

`docker image prune` 只清理未使用镜像，不会替代数据备份。
