# static-sync

把配置好的静态文件同步到本地目录，由宿主机已有的 nginx 对外发布。容器里只有同步进程，没有 nginx。

默认示例会同步 sing-box 官方规则集（`.srs`）。也可以按同样方式同步任意 URL、GitHub 仓库路径，或一份明确列出的站点文件。不限规则集。

## 行为

- **启动即同步**。`run_on_startup: true`（默认）时，容器起来就拉文件并落盘，不等待任何人访问。
- 之后按 cron 再同步。默认 **每天北京时间 05:00**（`0 5 * * *`，`Asia/Shanghai`）。
- nginx **只读磁盘**。文件还没同步到时返回 404，不会按请求回源。
- 某个文件下载失败时，**保留上一次成功的文件**。

## 快速开始

```bash
docker compose up -d --build
```

- 配置：`./config`（只读挂进容器）
- 数据：`./data/public`（给宿主机 nginx 用）
- 状态：`./data/state/status.json`
- 状态接口只绑在宿主机 `127.0.0.1:8080`，不要把 8080 暴露到公网

看同步结果：

```bash
curl -s http://127.0.0.1:8080/status
curl -s http://127.0.0.1:8080/health
```

首轮同步在后台进行。`/status` 里对应分组的 `ok` 变为 `true` 后，文件才在 `data/public/` 下。

可选环境变量 `GITHUB_TOKEN`，只用于提高 GitHub API 限额，不会写进公开目录或 `/status`。

## 配置

`config/defaults.yaml` 是全局默认（时区、cron、大小限制、监听地址）。其余每个 `*.yaml` 是一组任务：

| 文件 | 作用 |
|------|------|
| `defaults.yaml` | 全局默认，不单独成组 |
| `sing-box.yaml` | 默认同步官方 SRS 到 `data/public/sing-box/` |
| `site-example.yaml` | 站点 / GitHub 示例，**默认 `enabled: false`** |

新增一组任务：复制一份 yaml 即可，例如 `docs.yaml`。输出目录默认等于文件名（`docs.yaml` → `data/public/docs/`），可用 `output_dir` 覆盖。单文件可写自己的 `schedule`，没写的字段继承 defaults。`enabled: false` 整组跳过。

改完配置后重启容器：`docker compose up -d`。

### 三种 source

`url`：下载单个文件，`dest` 是相对该组目录的文件路径。

```yaml
sources:
  - id: geosite-cn
    type: url
    url: "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-cn.srs"
    dest: "geosite-cn.srs"
```

`github`：列出仓库里某个路径，再按 `include`（glob，可省略）下载。`dest: "."` 表示写到该组目录下，并保留相对路径。单次最多 500 个文件。

```yaml
sources:
  - id: geosite
    type: github
    repo: "owner/repo"
    ref: "main"
    path: "geo/geosite"
    include: ["*.srs"]
    dest: "."
```

`site`：只下载 `paths` 里写明的路径，不会爬整站。路径不能跳出 `base_url`。

```yaml
sources:
  - id: assets
    type: site
    base_url: "https://example.com/"
    paths: ["css/app.css", "js/app.js"]
    dest: "."
```

限制（在 `defaults.yaml` 的 `sync` 下）：单文件 `max_bytes`（默认 100MB）、超时、最多重定向次数。只允许 `http`/`https`，并拒绝指向本机、私网、链路本地和云元数据地址的 URL。

## 宿主机 nginx

示例在 [deploy/nginx-example.conf](deploy/nginx-example.conf)。把 `/opt/static-sync` 换成这台机器上的项目目录，再放进已有的 `server { }`。

同步完成后可以访问：

- `https://your.domain/static-sync/sing-box/geosite-cn.srs`
- `https://your.domain/sync-status`
- `https://your.domain/sync-health`

sing-box 远程规则集：

```json
{
  "type": "remote",
  "tag": "geosite-cn",
  "format": "binary",
  "url": "https://your.domain/static-sync/sing-box/geosite-cn.srs",
  "update_interval": "1d"
}
```

## 本地运行（不经 Docker）

需要 Python 3.12。

```bash
pip install -r syncer/requirements.txt
```

PowerShell：

```powershell
$env:CONFIG_DIR = "D:\projects\static-sync\config"
$env:DATA_DIR = "D:\projects\static-sync\data\public"
$env:STATE_DIR = "D:\projects\static-sync\data\state"
python -m app.main
```

在 `syncer` 目录里执行最后一条。`DATA_DIR` / `STATE_DIR` / `BIND` / `PORT` 会覆盖 yaml。

容器默认以 root 启动，创建数据目录后降权到 uid `1000`（用户 `syncer`）。如果之前用别的用户写过 `data/`，出现权限错误时把该目录交给 uid 1000。

## 测试

```bash
pip install -r syncer/requirements.txt
python -m unittest discover -s syncer/tests -t syncer
```
