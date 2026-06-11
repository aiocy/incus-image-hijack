#!/bin/bash
# ============================================================================
# Incus Image Hijack - One-click Installer
# ============================================================================
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/aiocy/incus-image-hijack/main/install.sh | bash
# ============================================================================
set -euo pipefail

# ---- Config ----
MIRROR_DOMAIN="sgp1mirror01.do.images.linuxcontainers.org"
IMAGE_SRC_URL="https://github.com/aiocy/incus-image-hijack/releases/download/v1.0.0"
INSTALL_DIR="/opt/image-hijack"
SERVER_PORT="443"

# Alpine 3.21 cloud image paths (relative to INSTALL_DIR/images/)
ALPINE_VERSION="3.21"
ALPINE_DATE="20260607_13:00"
ALPINE_DIR="images/alpine/${ALPINE_VERSION}/amd64/cloud/${ALPINE_DATE}"

# ---- Colors ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[✗]${NC} $1"; }

# ---- Preflight ----
if [[ $EUID -ne 0 ]]; then
    err "Must be run as root"
    exit 1
fi

echo "=========================================="
echo " Incus Image Hijack Installer"
echo "=========================================="

# ---- Dependencies ----
for cmd in python3 openssl nft curl host; do
    if ! command -v "$cmd" &>/dev/null; then
        err "Missing: $cmd"
        exit 1
    fi
done
info "All dependencies found"

# ---- Resolve mirror IPs ----
echo "Resolving $MIRROR_DOMAIN..."
MIRROR_IPV4=$(host "$MIRROR_DOMAIN" 2>/dev/null | awk '/has address/ {print $NF}' | head -1)
MIRROR_IPV6=$(host "$MIRROR_DOMAIN" 2>/dev/null | awk '/has IPv6 address/ {print $NF}' | head -1)

if [[ -z "$MIRROR_IPV4" && -z "$MIRROR_IPV6" ]]; then
    # Fallback to known IPs (for this mirror)
    MIRROR_IPV4="139.59.230.173"
    MIRROR_IPV6="2400:6180:0:d2:0:2:eacc:1000"
    warn "DNS resolution failed, using known IPs: $MIRROR_IPV4"
fi
info "Mirror IPv4: ${MIRROR_IPV4:-none}"
info "Mirror IPv6: ${MIRROR_IPV6:-none}"

# ---- Create directories ----
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/$ALPINE_DIR"
info "Created $INSTALL_DIR"

# ---- Download image files ----
echo "Downloading Alpine ${ALPINE_VERSION} cloud image..."
for f in incus.tar.xz meta.tar.xz rootfs.squashfs; do
    target="$INSTALL_DIR/$ALPINE_DIR/$f"
    if [[ -f "$target" ]]; then
        info "Already have $f ($(du -h "$target" | cut -f1))"
        continue
    fi
    echo -n "  $f ... "
    if curl -fsSL -o "$target" "$IMAGE_SRC_URL/$f" --max-time 120; then
        echo -e "${GREEN}done${NC} ($(du -h "$target" | cut -f1))"
    else
        err "Failed to download $f"
        exit 1
    fi
done
info "Image files ready"

# ---- Generate CA & server cert ----
CA_DIR="$INSTALL_DIR"
if [[ ! -f "$CA_DIR/server.pem" ]]; then
    echo "Generating self-signed CA and server certificate..."
    openssl genrsa -out "$CA_DIR/ca.key" 2048
    openssl req -x509 -new -nodes -key "$CA_DIR/ca.key" -sha256 -days 3650 \
        -out "$CA_DIR/ca.pem" -subj '/CN=HijackCA'

    openssl genrsa -out "$CA_DIR/server.key" 2048
    openssl req -new -key "$CA_DIR/server.key" -out "$CA_DIR/server.csr" \
        -subj "/CN=${MIRROR_DOMAIN}"
    openssl x509 -req -in "$CA_DIR/server.csr" -CA "$CA_DIR/ca.pem" \
        -CAkey "$CA_DIR/ca.key" -CAcreateserial -out "$CA_DIR/server.pem" \
        -days 3650 -sha256 \
        -extfile <(printf "subjectAltName=DNS:${MIRROR_DOMAIN}")
    rm -f "$CA_DIR/server.csr" "$CA_DIR/ca.srl"
    info "Certificates generated (CA + server)"
else
    info "Certificates already exist"
fi

# ---- Install CA to system trust ----
if ! grep -q "HijackCA" /etc/ssl/certs/ca-certificates.crt 2>/dev/null; then
    cp "$CA_DIR/ca.pem" /usr/local/share/ca-certificates/hijack-ca.crt
    update-ca-certificates --fresh >/dev/null 2>&1
    info "CA added to system trust"
else
    info "CA already trusted"
fi

# ---- Write server.py ----
cat > "$INSTALL_DIR/server.py" << 'PYEOF'
#!/usr/bin/env python3
"""Incus Image Hijack Server"""
import http.server, ssl, sys, os
PORT = int(os.environ.get("HIJACK_PORT", "443"))
ROOT = os.environ.get("HIJACK_ROOT", "/opt/image-hijack")
CERT = os.environ.get("HIJACK_CERT", "/opt/image-hijack/server.pem")
KEY = os.environ.get("HIJACK_KEY", "/opt/image-hijack/server.key")
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)
    def log_message(self, f, *a):
        sys.stderr.write("[HIJACK] %s %s %s\n" % a); sys.stderr.flush()
d = http.server.HTTPServer(("127.0.0.1", PORT), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)
d.socket = ctx.wrap_socket(d.socket, server_side=True)
print("[HIJACK] Running on :%d" % PORT, flush=True)
try: d.serve_forever()
except KeyboardInterrupt: d.shutdown()
PYEOF
chmod +x "$INSTALL_DIR/server.py"
info "server.py written"

# ---- Write systemd service ----
cat > /etc/systemd/system/image-hijack.service << 'UNIT'
[Unit]
Description=Incus Image Hijack Server
Documentation=https://github.com/aiocy/incus-image-hijack
After=network.target
[Service]
ExecStart=/usr/bin/env python3 /opt/image-hijack/server.py
Restart=always
RestartSec=3
User=root
Environment=HIJACK_PORT=443
Environment=HIJACK_ROOT=/opt/image-hijack
Environment=HIJACK_CERT=/opt/image-hijack/server.pem
Environment=HIJACK_KEY=/opt/image-hijack/server.key
NoNewPrivileges=yes
ReadWritePaths=/opt/image-hijack
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable image-hijack.service
systemctl restart image-hijack.service
sleep 2
if systemctl is-active --quiet image-hijack.service; then
    info "image-hijack.service is running"
else
    err "Service failed to start, check: journalctl -u image-hijack.service"
    exit 1
fi

# ---- Add nftables DNAT rules ----
nft add table ip hijack 2>/dev/null || true
nft add chain ip hijack output '{ type nat hook output priority -100; policy accept; }' 2>/dev/null || true

if [[ -n "$MIRROR_IPV4" ]]; then
    # Flush old rule for this IP first, then add
    nft flush chain ip hijack output 2>/dev/null || true
    nft add rule ip hijack output ip daddr "$MIRROR_IPV4" tcp dport "$SERVER_PORT" dnat to 127.0.0.1
    info "nftables DNAT IPv4: $MIRROR_IPV4:$SERVER_PORT -> 127.0.0.1"
fi

# IPv6 table
nft add table ip6 hijack 2>/dev/null || true
nft add chain ip6 hijack output '{ type nat hook output priority -100; policy accept; }' 2>/dev/null || true
if [[ -n "$MIRROR_IPV6" ]]; then
    nft flush chain ip6 hijack output 2>/dev/null || true
    nft add rule ip6 hijack output ip6 daddr "$MIRROR_IPV6" tcp dport "$SERVER_PORT" dnat to ::1
    info "nftables DNAT IPv6: $MIRROR_IPV6 -> [::1]"
fi

# Also add PREROUTING rules (for traffic passing through this machine as a gateway)
nft add chain ip hijack prerouting '{ type nat hook prerouting priority -100; policy accept; }' 2>/dev/null || true
if [[ -n "$MIRROR_IPV4" ]]; then
    nft add rule ip hijack prerouting ip daddr "$MIRROR_IPV4" tcp dport "$SERVER_PORT" dnat to 127.0.0.1
    info "nftables DNAT PREROUTING: $MIRROR_IPV4 -> 127.0.0.1"
fi

nft add chain ip6 hijack prerouting '{ type nat hook prerouting priority -100; policy accept; }' 2>/dev/null || true
if [[ -n "$MIRROR_IPV6" ]]; then
    nft add rule ip6 hijack prerouting ip6 daddr "$MIRROR_IPV6" tcp dport "$SERVER_PORT" dnat to ::1
    info "nftables DNAT PREROUTING IPv6: $MIRROR_IPV6 -> [::1]"
fi

# ---- Persist nftables rules ----
nft list ruleset > /etc/nftables.conf
systemctl enable nftables 2>/dev/null || true
info "nftables rules persisted"

# ---- Verify ----
echo ""
echo "=========================================="
echo " Verification"
echo "=========================================="
# Test local HTTPS server
if curl -sk --max-time 5 "https://${MIRROR_DOMAIN}/images/alpine/${ALPINE_VERSION}/amd64/cloud/${ALPINE_DATE}/incus.tar.xz" \
    -o /dev/null -w '%{http_code}' 2>/dev/null | grep -q 200; then
    info "Hijack working! HTTP 200 from local server"
else
    warn "Local hijack test failed - check service status"
fi

echo ""
echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN} Incus Image Hijack installed!${NC}"
echo -e "${GREEN}==========================================${NC}"
echo ""
echo "  Install dir: $INSTALL_DIR"
echo "  CA cert:     $INSTALL_DIR/ca.pem"
echo "  Server cert: $INSTALL_DIR/server.pem"
echo ""
echo "  To test:"
echo "    curl -sk https://${MIRROR_DOMAIN}/images/alpine/${ALPINE_VERSION}/amd64/cloud/${ALPINE_DATE}/incus.tar.xz -o /dev/null -w '%{http_code}'"
echo ""
echo "  To uninstall:"
echo "    systemctl stop image-hijack && systemctl disable image-hijack"
echo "    nft delete table ip hijack; nft delete table ip6 hijack"
echo "    rm -rf /opt/image-hijack /etc/systemd/system/image-hijack.service"
echo "    rm /usr/local/share/ca-certificates/hijack-ca.crt && update-ca-certificates --fresh"
echo "    nft list ruleset > /etc/nftables.conf"
echo ""
