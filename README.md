# Incus Image Hijack

当你的 Incus 计算节点因镜像站 IP 限制无法下载系统镜像时，通过 **nftables DNAT + 本地 HTTPS 服务器** 将请求劫持到本地缓存。

## 适用场景

- 托管 Incus 平台（如 IncusHlii）**硬编码**了特定镜像站 URL（例如 `sgp1mirror01.do.images.linuxcontainers.org`）
- 镜像站限制了来源 IP（例如仅允许 DigitalOcean 内网访问）
- WARP 等代理也无法绕过限制
- 平台不提供修改镜像源配置的功能

## 原理

```
Incus/平台请求 ──https://sgp1mirror01.do.images.linuxcontainers.org/──→ 🌐 互联网
                                                                         ↓
                                                               [nftables DNAT]
                                                           OUTPUT + PREROUTING
                                                                         ↓
                                                               127.0.0.1:443
                                                                         ↓
                                                             [image-hijack.service]
                                                           Python HTTPS 服务器
                                                    (HijackCA 自签名证书 + 预缓存镜像)
```

1. **nftables DNAT** 截获发往镜像站 IP 的 `:443` 流量，重定向到 `127.0.0.1:443`
2. **Python HTTPS 服务器** 使用 `HijackCA` 签发的证书（含正确 SNI 域名）回应
3. **预下载的镜像文件**（`incus.tar.xz` + `rootfs.squashfs`）直接从本地返回
4. **HijackCA 已加入系统信任链**，TLS 验证通过 ✅

## 快速安装

```bash
curl -fsSL https://raw.githubusercontent.com/aiocy/incus-image-hijack/main/install.sh | bash
```

脚本自动完成：
- ✅ 检测依赖（python3, openssl, nft, curl, host）
- ✅ 解析镜像站 IP（DNS 失败则 fallback 到已知 IP）
- ✅ 从 GitHub Release 下载 Alpine 3.21 cloud 镜像
- ✅ 生成 HijackCA 自签名 CA + 服务器证书
- ✅ 将 CA 加入系统信任链（`update-ca-certificates`）
- ✅ 部署 HTTPS 服务器（systemd 自启）
- ✅ 添加 nftables DNAT 规则（OUTPUT + PREROUTING）
- ✅ 持久化规则到 `/etc/nftables.conf`
- ✅ 验证劫持是否生效

## 手动验证

```bash
curl -sk https://sgp1mirror01.do.images.linuxcontainers.org/images/alpine/3.21/amd64/cloud/20260607_13:00/incus.tar.xz -o /dev/null -w '%{http_code}'
```

返回 `200` 表示劫持成功。

## 文件结构

```
/opt/image-hijack/
├── server.py          # HTTPS 服务器
├── server.pem         # 服务端证书（CN=镜像站域名）
├── server.key         # 服务端私钥
├── ca.pem             # 自签名 CA 证书（HijackCA）
├── ca.key             # CA 私钥
└── images/            # 预缓存镜像文件
    └── alpine/3.21/amd64/cloud/20260607_13:00/
        ├── incus.tar.xz      # 容器元数据
        ├── meta.tar.xz       # 元数据
        └── rootfs.squashfs   # 根文件系统（20MB）
```

## ⚠️ 硬编码说明

当前版本为 **Alpine 3.21 cloud amd64** 定制，以下为硬编码项，修改前请确认：

| 硬编码项 | 默认值 | 说明 |
|----------|--------|------|
| 镜像站域名 | `sgp1mirror01.do.images.linuxcontainers.org` | 被劫持的目标域名 |
| 镜像站 IPv4 | `139.59.230.173` | DNAT 拦截目标，DNS 失败时 fallback |
| 镜像站 IPv6 | `2400:6180:0:d2:0:2:eacc:1000` | DNAT 拦截目标，DNS 失败时 fallback |
| 镜像类型 | `Alpine 3.21 cloud x86_64` | 只预缓存了这一个镜像 |
| 监听端口 | `443` | HTTPS 服务器端口 |
| 安装路径 | `/opt/image-hijack` | 所有文件部署路径 |
| 镜像下载源 | GitHub Release v1.0.0 | 预缓存镜像文件的来源 |

### 适配其他镜像

1. 从 `images.linuxcontainers.org`（或可达的镜像站）下载对应镜像的 `incus.tar.xz` + `meta.tar.xz` + `rootfs.squashfs`
2. 放入 `/opt/image-hijack/images/` 下对应的目录结构
3. `server.py` 的 `SimpleHTTPRequestHandler` 自动映射目录，无需改代码

### 适配其他镜像站

1. 修改 `install.sh` 中的 `MIRROR_DOMAIN` 和对应的 IP
2. 重新生成证书（`subjectAltName` 要匹配新域名）
3. 重新安装 nftables DNAT 规则

## 卸载

```bash
systemctl stop image-hijack && systemctl disable image-hijack
nft delete table ip hijack
nft delete table ip6 hijack
rm -rf /opt/image-hijack /etc/systemd/system/image-hijack.service
rm /usr/local/share/ca-certificates/hijack-ca.crt
update-ca-certificates --fresh
nft list ruleset > /etc/nftables.conf
```

## Auto-Update (v2.0)

When Incus requests a new Alpine image version that isn't cached yet, the
hijack **automatically downloads, SSH-patches, and caches it** — no manual
intervention needed.

### How it works

1. **`server.py`** — enhanced HTTPS server that on cache miss triggers
   `auto_update.py` to fetch the new image from the upstream mirror.
2. **`auto_update.py`** — standalone script that:
   - Temporarily disables the nftables DNAT to reach the real upstream
   - Downloads `incus.tar.xz`, `rootfs.squashfs`, `meta.tar.xz`
   - Unpacks rootfs, installs `openssh-server`, configures SSH, repacks
   - Caches the patched files
   - Restores DNAT and restarts the hijack

### Cron (recommended)

A daily cron job ensures new images are downloaded proactively:

```bash
0 6 * * * /usr/bin/python3 /opt/image-hijack/auto_update.py >> /var/log/image-hijack-auto-update.log 2>&1
```

### Manual run

```bash
# Check for and download latest Alpine
/opt/image-hijack/auto_update.py

# Or specify a specific serial
/opt/image-hijack/auto_update.py 20260620_13:00
```
