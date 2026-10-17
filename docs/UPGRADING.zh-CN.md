# 旧版本升级与迁移

## Termux 安装时 curl 提示 CANNOT LINK EXECUTABLE

如果出现 `cannot locate symbol "ngtcp2_conn_get_tls_early_data_rejected2"` 或类似错误，说明新安装的 `curl/libcurl` 与现有动态库版本不匹配。如果错误出现在下载脚本这一步，`vertex` 管理命令也尚未安装。

1. 在 Termux 的 `$` 提示符后运行：

   ```bash
   apt update && apt full-upgrade -y
   ```

   这会更新 Termux 已安装的软件包。若询问是否替换修改过的配置文件，选 `N` 保留现有配置。使用 `apt` 是因为 `pkg` 的镜像检查也依赖当前无法执行的 `curl`。
2. 等升级完成、再次出现 `$` 后，运行 [README 的一键安装命令](../README.md#手机一键部署android-termux)。新版安装入口会在安装依赖前完整更新 Termux 软件包，避免只升级 `curl`。
3. 安装成功后再运行 `vertex status`。旧手动部署的数据按下文迁移。

这是 [Termux 官方 pkg 脚本](https://github.com/termux/termux-tools/blob/master/scripts/pkg.in) 在基础命令无法执行时给出的恢复方式。若 `apt` 本身也报错，保留其报错信息继续排查。

## 已经使用这次的 Termux 一键安装器

在 Termux 输入：

```bash
vertex update
```

脚本先停止正在运行的服务并备份配置，然后拉取本仓库的更新、按需安装依赖，最后恢复原来的运行状态。原本未启动的服务，更新后执行 `vertex start` 即可。

- Google Key、Cookie、模型设置：`$HOME/.quick-vertex-api/web_state.json`。
- 本服务 API Key：`$HOME/.quick-vertex-api/service.env`。
- 升级前备份：`$HOME/.quick-vertex-api/backups/日期时间/`，其中 `commit.txt` 记录原版本。
- 更新不会重新生成 API Key，客户端地址与原密钥可以继续使用。升级后首页复制的密钥会统一显示为 `ksv-` 格式；旧客户端继续使用原密钥也兼容。

检查升级结果：

```bash
vertex status
vertex open
```

若提示本地源码有修改，先保存这些修改；脚本会停止更新，保留文件。网络失败可再次运行 `vertex update`；运行异常可用 `vertex logs` 查看原因。手机浏览器仍显示旧样式时，刷新页面。

### 恢复升级前的配置

先 `vertex stop`，把对应备份目录中的 `web_state.json`、`service.env` 复制回 `$HOME/.quick-vertex-api/`，再 `vertex start`。只恢复你确认需要的那次备份。这会恢复配置，程序源码版本不变。

## 旧版是手动在 Termux 运行的，还没有 vertex 命令

这属于首次迁移，不能直接执行 `vertex update`。

1. 在旧服务的终端按 `Ctrl+C` 停止服务。先记下旧版设置的 `API_KEY` 和 `STATE_DIR`；没有指定 `STATE_DIR` 时，通常使用启动时所在目录。
2. 备份旧数据目录中的 `web_state.json`，以及原来的 `.env` 或启动脚本。Key、Cookie 和设置可能分别位于状态文件和环境变量中，因此两者都要留存。
3. 运行 [README 的 Termux 一键安装命令](../README.md#手机一键部署android-termux)。它自动准备独立环境，不要求旧版已经安装了哪些依赖。
4. 安装完成后执行 `vertex stop`，把旧状态文件复制到新数据目录。下面以旧文件位于 `$HOME/vertex2openai/data/web_state.json` 为例，请把 `OLD_STATE` 改成你实际确认过的旧数据目录：

   ```bash
   OLD_STATE="$HOME/vertex2openai/data"
   cp -a "$HOME/.quick-vertex-api" "$HOME/.quick-vertex-backup-$(date +%Y%m%d-%H%M%S)"
   test -f "$OLD_STATE/web_state.json" && cp "$OLD_STATE/web_state.json" "$HOME/.quick-vertex-api/web_state.json"
   ```

5. 若要继续使用原来的客户端密钥，把新目录 `service.env` 的 `API_KEY=` 改成原来的值；也可以保留新生成的 `ksv-` 密钥，稍后从控制台首页复制到客户端。旧的无前缀密钥仍可直接使用，首页会提供统一的 `ksv-` 格式。不要用旧 `.env` 覆盖整个 `service.env`，新文件还包含正确的数据目录和启动设置。
6. 执行 `vertex start`，打开 `http://127.0.0.1:8050`，检查「通道与凭证」和「模型参数」。原来只通过环境变量提供、尚未写入状态文件的 Google Key 或 Cookie，需要在页面重新填写一次。

确认新版本正常后再处理旧目录；迁移过程不需要卸载旧环境。如果旧版位于另一个 PRoot 环境，先从那个环境导出状态文件到 Termux 主目录，再按上述步骤复制。

## 旧版通过 Docker Compose 部署

先确认当前项目从你的仓库 `PlayerAlex/vertex2openai` 获取更新，并备份 `data/`、`.env` 与自定义的 Compose 文件。保持原来的数据卷路径和 `API_KEY`。

在项目目录执行：

```bash
git pull --ff-only
docker compose up -d --build
docker compose ps
```

如果使用发布镜像而非本地构建：

```bash
docker compose pull
docker compose up -d
```

如果旧目录仍关联原作者仓库，应先从你的仓库获取新版代码，再复用原数据目录和配置。Git 提示冲突或不能快进时，先处理本地修改，不要强制覆盖配置。

容器未挂载数据目录的旧部署，需要在重建前先从旧容器导出状态文件；重建后再补挂载不能恢复已经丢失的数据。更新完成后，手机和电脑仍使用原访问地址，页面首页可复制 Base URL 和密钥。
