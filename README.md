# Incus Image Hijack

当你的 Incus 计算节点无法访问官方镜像站（如 `sgp1mirror01.do.images.linuxcontainers.org`）时，通过本地 HTTPS 劫持 + nftables DNAT 将请求重定向到本地缓存。

## 原理

```
Incus / 平台 ──https://sgp1mirror01.do.images.linuxcontainers.org/──→ 🌐 互联网
                                                                        ↓
                                                              [nftables DNAT]
                                                                        ↓
                                                              127.0.0.1:443
                                                                        ↓
                                                            [image-hijack.service]
                                                          Python HTTPS Server
                                                          (自签名证书 + 预缓存镜像)
```

1. **nftables DNAT** 截获发往镜像站 IP 的 443 流量，重定向到 `127.0.0.1:443`
2. **Python HTTPS Server** 使用 `HijackCA` 签发的证书（含正确 SNI）回应
3. **预下载的镜像文件**（incus.tar.xz + rootfs.squashfs）直接返回给客户端
4. **系统信任链** 包含 `HijackCA`，所以 TLS 验证通过

## 快速安装

```bash
curl -fsSL https://raw.githubusercontent.com/aiocy/incus-image-hijack/main/install.sh | bash
```

脚本会自动：
- ✓ 检测依赖
- ✓ 下载 Alpine 3.21 cloud 镜像文件
- ✓ 生成自签名 CA 和服务端证书
- ✓ 将 CA 加入系统信任链
- ✓ 部署 HTTPS 服务器（systemd 自启）
- ✓ 添加 nftables DNAT 规则（OUTPUT + PREROUTING）
- ✓ 持久化规则到 `/etc/nftables.conf`

## 手动测试

安装完成后，验证劫持是否生效：

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
├── ca.pem             # 自签名 CA 证书
├── ca.key             # CA 私钥
└── images/            # 预缓存镜像文件
    └── alpine/3.21/amd64/cloud/20260607_13:00/
        ├── incus.tar.xz
        ├── meta.tar.xz
        └── rootfs.squashfs
```

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

## 支持的镜像

| 镜像 | 版本 | 格式 |
|------|------|------|
| Alpine Linux | 3.21 cloud amd64 | incus.tar.xz + rootfs.squashfs |

如需其他镜像，请在 [Issues](https://github.com/aiocy/incus-image-hijack/issues) 中提出。
