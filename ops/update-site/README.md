# DcmGet 静态更新源运维文件

这些文件用于 `dcmget.v2ex.com.cn` 的最小静态更新源部署，目标是给 Windows 安装版自动更新提供只读 HTTPS 源站内容。

## 目标布局

站点根目录应为：

```text
/opt/1panel/www/sites/dcmget.v2ex.com.cn/index
```

容器内对应：

```text
/www/sites/dcmget.v2ex.com.cn/index
```

站点发布后应至少包含：

```text
index.html
updates/
├── stable/
│   └── UPDATE-MANIFEST.signed.json
└── releases/
    └── <version>/
        └── <artifact files>
```

## OpenResty 配置

- 首次申请证书使用：`dcmget.v2ex.com.cn.http.conf`
- 证书签发后切换为：`dcmget.v2ex.com.cn.https.conf`
- 目标宿主机路径：`/opt/1panel/www/conf.d/dcmget.v2ex.com.cn.conf`
- 设计约束：
  - 只允许 `GET` / `HEAD`
  - 禁止目录浏览
  - `stable/UPDATE-MANIFEST.signed.json` 强制 `no-store/no-cache`
  - `updates/releases/**` 使用长缓存和 `immutable`
  - 提供 `GET /healthz` 返回 `200 ok`
  - 其余未命中的路径直接 `404`

## 部署顺序

1. 在宿主机创建目录：

   ```bash
   /opt/1panel/www/sites/dcmget.v2ex.com.cn/index
   /opt/1panel/www/sites/dcmget.v2ex.com.cn/logs
   /opt/1panel/www/sites/dcmget.v2ex.com.cn/index/updates/stable
   /opt/1panel/www/sites/dcmget.v2ex.com.cn/index/updates/releases
   ```

2. 上传 `index.html` 到站点根目录。
3. 上传 `dcmget.v2ex.com.cn.http.conf` 到 `/opt/1panel/www/conf.d/dcmget.v2ex.com.cn.conf`。
4. 在 OpenResty 容器内执行配置检查后再 reload。
5. 先用主机 IP + Host 头验证：

   ```bash
   curl -H 'Host: dcmget.v2ex.com.cn' http://144.34.233.165/healthz
   ```

6. 再切换 DNS。

## Cloudflare DNS 快速更新（CLI）

脚本：`ops/update-site/upsert_cloudflare_dns.py`

- 默认参数（按你的要求）：
  - zone：`v2ex.com.cn`
  - 记录：`dcmget.v2ex.com.cn`
  - A 记录：`144.34.233.165`
  - `proxied=true`
  - ttl：`1`（Auto）
- 使用方式：
  - `CLOUDFLARE_API_TOKEN=... python3 ops/update-site/upsert_cloudflare_dns.py --dry-run`
  - 去掉 `--dry-run` 即执行写入
- 结果会输出 `created` / `updated` / `unchanged`
- 规则：仅处理 A 记录；检测到同名多条 A 记录会直接失败退出

## HTTPS 与证书续期

先部署 HTTP 模板并确认 `/.well-known/acme-challenge/` 可访问，再在 `bwg-snell` 上使用 acme.sh 的 webroot 模式签发证书。以下命令仅为运维步骤，本仓库不会自动执行：

```bash
mkdir -p /opt/1panel/www/sites/dcmget.v2ex.com.cn/ssl

~/.acme.sh/acme.sh --issue \
  --server letsencrypt \
  -d dcmget.v2ex.com.cn \
  -w /opt/1panel/www/sites/dcmget.v2ex.com.cn/index

~/.acme.sh/acme.sh --install-cert \
  -d dcmget.v2ex.com.cn \
  --fullchain-file /opt/1panel/www/sites/dcmget.v2ex.com.cn/ssl/fullchain.pem \
  --key-file /opt/1panel/www/sites/dcmget.v2ex.com.cn/ssl/privkey.pem \
  --reloadcmd 'docker exec 1Panel-openresty-dPej openresty -t && docker exec 1Panel-openresty-dPej openresty -s reload'
```

证书安装成功后，将 `dcmget.v2ex.com.cn.https.conf` 部署到目标配置路径，并先检查再重载：

```bash
docker exec 1Panel-openresty-dPej openresty -t
docker exec 1Panel-openresty-dPej openresty -s reload
curl -I https://dcmget.v2ex.com.cn/healthz
```

`--install-cert` 会把 reload 命令保存到 acme.sh 的续期配置中；不要直接引用 `~/.acme.sh/` 下的临时证书文件。

## 发布 Windows 更新

### 日常快速组件更新（默认）

日常代码和界面迭代使用独立的 `Windows Component Update` 工作流。它只构建 onedir 应用负载、生成最近最多 5 个稳定版本直达当前版本的组件补丁、使用独立 Ed25519 更新密钥签名清单，并生成供下一次构建使用的 `component-baseline.zip`。它不会构建完整安装发布物，也不会执行完整安装升级验收，因此反馈更快。

```bash
gh workflow run windows-component-update.yml \
  --repo ge2009/dcmget \
  --ref main \
  -f version=3.6.1 \
  -f publish_update=false
```

- `publish_update=false` 是默认值，只生成 GitHub Actions 验收产物。
- 明确改为 `publish_update=true` 时，才会创建不可覆盖的 `component-vX.Y.Z` GitHub Release；正式完整发布仍独占 `vX.Y.Z` 标签。
- 工作流在发布前读取 `DCMGET_UPDATE_SIGNING_PRIVATE_KEY_BASE64`；私钥只存在于 GitHub Actions secret，不进入源码、发布目录或镜像服务器。
- `component-baseline.zip` 只供后续构建器比对应用树，不进入签名更新清单的 `artifacts`，客户端不会下载它。
- 更新清单可以不包含完整安装包，但必须包含至少一个经过严格校验的组件补丁。没有匹配基线的旧客户端需要人工安装新的完整版本。
- 自动更新链必须先有一个可信完整发布作为初始基线；仓库尚无合格基线时，组件工作流会明确失败。

只有下列情况才运行完整的 `Windows Release` 工作流：首次建立签名基线、安装逻辑或依赖布局变化、需要给新用户提供安装包，或者需要覆盖超过最近 5 个版本的升级场景。

完整工作流的 `component_update` 入口会从 GitHub `latest` 读取上一份可信基线：组件发布使用 `component-baseline.zip`，完整发布使用 `DcmGet-<version>-windows-x64.zip`。两种情况都必须先验证 Ed25519 签名信封，并确认 `vX.Y.Z` 或 `component-vX.Y.Z` 标签与签名清单版本完全一致，因此不需要为了避开组件版 `latest` 而关闭组件差分。

静态更新镜像中的同一版本目录是不可变的。某个 `X.Y.Z` 组件更新一旦发布到镜像，后续如需完整安装包，请将完整发布版本提升到更高的三段式版本（例如 `X.Y.(Z+1)`），不要尝试用完整包覆盖同版本目录。

```bash
gh workflow run windows-release.yml \
  --repo ge2009/dcmget \
  --ref main \
  -f version=3.7.0 \
  -f publish_release=true \
  -f component_update=true
```

### 发布到静态镜像

`publish_windows_release.sh` 接受单个版本的扁平发布目录和严格的三段式版本号。目录必须包含 `UPDATE-MANIFEST.json` 与 `UPDATE-MANIFEST.signed.json`，并且只允许顶层普通文件；不要直接传入混有历史产物和子目录的仓库 `release/windows/`。

```bash
mkdir -p /tmp/dcmget-3.6.1-release
gh run download <run-id> \
  --repo ge2009/dcmget \
  --name DcmGet-3.6.1-GitHub-Release \
  --dir /tmp/dcmget-3.6.1-release
./ops/update-site/publish_windows_release.sh /tmp/dcmget-3.6.1-release 3.6.1
```

组件更新使用不含 builder baseline 的独立 Actions 产物：

```bash
mkdir -p /tmp/dcmget-3.6.1-update
gh run download <run-id> \
  --repo ge2009/dcmget \
  --name DcmGet-3.6.1-Component-Update \
  --dir /tmp/dcmget-3.6.1-update
./ops/update-site/publish_windows_release.sh /tmp/dcmget-3.6.1-update 3.6.1
```

不要把 `DcmGet-<version>-Component-Baseline` 下载到该目录；它只用于下一轮构建，不应发布到客户端静态镜像。

脚本固定通过 `ssh bwg-snell` 发布，步骤如下：

1. 使用仓库内置公钥验证 Ed25519 签名信封，并确认其内容与 `UPDATE-MANIFEST.json` 逐字节一致，再校验版本、所有声明资源的大小和 SHA-256。
2. 允许“一个完整安装包（可有或没有 Authenticode）”或“零个完整安装包但至少一个合法组件补丁”；拒绝空清单、未知资源类型和越界组件路径。
3. 使用 `rsync` 上传到 `updates/releases/` 下的同文件系统隐藏临时目录。
4. 使用本地 SHA-256 清单逐文件校验远端内容。
5. 原子改名为 `updates/releases/<version>/`；如果同版本已存在，仅在内容完全一致时继续。
6. 清理旧的语义版本目录，保留最新两个；当前 stable 对应版本和正在发布版本始终受保护。若无法唯一识别当前 stable，安全地跳过清理。
7. 最后单独上传、校验并原子替换单文件 `updates/stable/UPDATE-MANIFEST.signed.json`。

发布前需确保本机能够无交互访问 `ssh bwg-snell`，并安装 `ssh`、`scp`、`rsync` 及 `sha256sum` 或 `shasum`。脚本不负责签名清单，也不会修改 OpenResty 或 DNS。

## 发布注意事项

- 新版本资源先上传到 `updates/releases/<version>/`，确认完整后再覆盖 `updates/stable/UPDATE-MANIFEST.signed.json`。
- 由于 `stable` 清单是更新入口，最后一步再切换它，能把中断发布的影响限制在单次检查窗口内。
- 如果磁盘空间继续紧张，优先保留“当前稳定版 + 上一个稳定版”两个版本目录，删除更旧目录前先确认没有客户端仍在使用。
