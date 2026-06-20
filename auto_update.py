#!/usr/bin/env python3
"""
Incus Image Hijack — Auto-Update Script

Checks the upstream mirror for new Alpine image versions, downloads
them, patches rootfs.squashfs with SSH, and caches the result.

Can be run manually, via cron, or triggered by the hijack server.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────

ROOT = "/opt/image-hijack"
CACHE_DIR = Path(ROOT) / "images"
UPSTREAM_BASE = "https://sgp1mirror01.do.images.linuxcontainers.org"

# Which distro/arch to track
DISTRO = "alpine"
VERSION = "3.21"
ARCH = "amd64"
IMAGE_TYPE = "cloud"

# Same path structure as the upstream mirror
UPSTREAM_IMAGE_PATH = f"/cloud-images/{DISTRO}/{VERSION}/{ARCH}/{IMAGE_TYPE}"
LOCAL_IMAGE_DIR = CACHE_DIR / DISTRO / VERSION / ARCH / IMAGE_TYPE

# ── DNAT management ────────────────────────────────────────────────────

_DNAT_RULES_BACKUP = "/tmp/.hijack_dnat_bak"


def _dnat_save_and_disable():
    """Backup and flush the DNAT table so we can reach the real upstream."""
    r = subprocess.run(
        ["nft", "list", "table", "ip", "hijack"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return True  # no DNAT to worry about
    with open(_DNAT_RULES_BACKUP, "w") as f:
        f.write(r.stdout)
    # Temporarily remove output rules by deleting the table and recreating
    # a skeleton (empty) table in its place
    subprocess.run(["nft", "delete", "table", "ip", "hijack"],
                   capture_output=True, timeout=5)
    # Create empty table so hooks still exist but no rules
    subprocess.run(
        ["nft", "add", "table", "ip", "hijack"],
        capture_output=True, timeout=5,
    )
    return True


def _dnat_restore():
    """Restore DNAT rules from backup."""
    if os.path.exists(_DNAT_RULES_BACKUP):
        subprocess.run(["nft", "-f", _DNAT_RULES_BACKUP],
                       capture_output=True, timeout=5)
        os.unlink(_DNAT_RULES_BACKUP)


def _stop_hijack():
    subprocess.run(["systemctl", "stop", "image-hijack"],
                   capture_output=True, timeout=30)


def _start_hijack():
    subprocess.run(["systemctl", "start", "image-hijack"],
                   capture_output=True, timeout=30)


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[AUTO-UPDATE] {ts} {msg}", flush=True)


# ── Fetch helpers ──────────────────────────────────────────────────────

def download_file(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        log(f"Downloading {dest.name}...")
        urllib.request.urlretrieve(url, str(dest))
        size = dest.stat().st_size
        log(f"  → {size:,} bytes")
        return True
    except Exception as e:
        log(f"  ✗ {e}")
        return False


def get_upstream_serial() -> str | None:
    """
    Fetch the images.json index from upstream and find the latest
    Alpine 3.21 amd64 cloud serial.
    """
    url = f"{UPSTREAM_BASE}/streams/v1/images.json"
    log(f"Checking upstream index: {url}")
    try:
        req = urllib.request.urlopen(url, timeout=30)
        data = json.loads(req.read().decode())
        products = data.get("products", {})

        # Look for Alpine 3.21 amd64 cloud entries
        # Product key format: "alpine:3.21:amd64:cloud"
        target_key = f"{DISTRO}:{VERSION}:{ARCH}:{IMAGE_TYPE}"
        target_key2 = f"{DISTRO.capitalize()}:{VERSION}:{ARCH}:{IMAGE_TYPE}"
        for prod_key, prod in products.items():
            if prod_key.lower() != target_key and prod_key.lower() != target_key2.lower():
                continue
            versions = prod.get("versions", {})
            if not versions:
                continue
            # Versions dict keys are serials like "20260619_13:00"
            # Sort to get the latest
            sorted_serials = sorted(versions.keys(), reverse=True)
            if sorted_serials:
                log(f"  Found serials: {', '.join(sorted_serials[:3])}")
                return sorted_serials[0]
        return None
    except Exception as e:
        log(f"  ✗ Failed to fetch index: {e}")
        return None


def get_cached_serials() -> set[str]:
    """List serials we already have cached."""
    if not LOCAL_IMAGE_DIR.is_dir():
        return set()
    return {d.name for d in LOCAL_IMAGE_DIR.iterdir() if d.is_dir()}


# ── SSH patching ───────────────────────────────────────────────────────

def patch_rootfs(rootfs_path: Path) -> bool:
    """Unpack squashfs, install openssh-server, repack."""
    log(f"Patching {rootfs_path.name} with SSH...")
    tmpdir = tempfile.mkdtemp(prefix="hijack-patch-")
    try:
        # Unsquash
        r = subprocess.run(
            ["unsquashfs", "-d", f"{tmpdir}/root", str(rootfs_path)],
            capture_output=True, timeout=120,
        )
        if r.returncode != 0:
            log(f"  unsquashfs failed: {r.stderr.decode()[:200]}")
            return False

        # Mount /proc + /dev for chroot
        for mp in ["proc", "dev"]:
            os.makedirs(f"{tmpdir}/root/{mp}", exist_ok=True)
            subprocess.run(
                ["mount", "--bind", f"/{mp}", f"{tmpdir}/root/{mp}"],
                capture_output=True, timeout=10,
            )

        # Install openssh-server
        r = subprocess.run(
            ["chroot", f"{tmpdir}/root", "apk", "add", "openssh-server"],
            capture_output=True, timeout=120,
        )
        if r.returncode != 0:
            log(f"  apk add failed: {r.stderr.decode()[:200]}")
            return False

        # Configure SSH: permit root login
        sshd_cfg = f"{tmpdir}/root/etc/ssh/sshd_config"
        with open(sshd_cfg, "a") as f:
            f.write("\n# Added by hijack auto-update\n")
            f.write("PermitRootLogin yes\n")
            f.write("PasswordAuthentication yes\n")
            f.write("UseDNS no\n")

        # Enable sshd at boot
        subprocess.run(
            ["chroot", f"{tmpdir}/root", "rc-update", "add", "sshd"],
            capture_output=True, timeout=30,
        )

        # Generate host keys
        subprocess.run(
            ["chroot", f"{tmpdir}/root", "ssh-keygen", "-A"],
            capture_output=True, timeout=60,
        )

        # Repack squashfs
        r = subprocess.run(
            ["mksquashfs", f"{tmpdir}/root", str(rootfs_path),
             "-comp", "xz", "-noappend", "-b", "256K"],
            capture_output=True, timeout=300,
        )
        if r.returncode != 0:
            log(f"  mksquashfs failed: {r.stderr.decode()[:200]}")
            return False

        size = rootfs_path.stat().st_size
        log(f"  → Patched rootfs: {size:,} bytes")
        return True

    except Exception as e:
        log(f"  ✗ Patch error: {e}")
        return False
    finally:
        for mp in ["dev", "proc"]:
            try:
                subprocess.run(["umount", "-l", f"{tmpdir}/root/{mp}"],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Main update flow ───────────────────────────────────────────────────

def update(serial: str | None = None) -> bool:
    """
    Ensure a specific image serial is cached and SSH-patched.
    If serial is None, check upstream for the latest.
    """
    if serial is None:
        serial = get_upstream_serial()
        if not serial:
            log("No upstream serial found — skipping")
            return False

    cached = get_cached_serials()
    if serial in cached:
        log(f"Serial {serial} already cached — skipping")
        return True

    log(f"New serial detected: {serial}")
    log(f"Cached serials: {cached}")

    # Stop hijack and disable DNAT for download
    _stop_hijack()
    _dnat_save_and_disable()

    success = False
    try:
        serial_dir = LOCAL_IMAGE_DIR / serial
        serial_dir.mkdir(parents=True, exist_ok=True)

        base_upstream_url = f"{UPSTREAM_BASE}{UPSTREAM_IMAGE_PATH}/{serial}"

        # Download all 3 files
        files = ["incus.tar.xz", "rootfs.squashfs", "meta.tar.xz"]
        downloads_ok = True
        for fname in files:
            url = f"{base_upstream_url}/{fname}"
            dest = serial_dir / fname
            if not download_file(url, dest):
                downloads_ok = False
                break

        if not downloads_ok:
            log("Download failed — cleaning up")
            shutil.rmtree(serial_dir, ignore_errors=True)
            success = False
        else:
            # Patch rootfs
            rootfs = serial_dir / "rootfs.squashfs"
            if rootfs.exists():
                if patch_rootfs(rootfs):
                    log(f"✅ Serial {serial} ready!")
                    success = True
                else:
                    log("Patch failed — keeping unpatched cache (better than nothing)")
                    # Keep the raw files so at least the image works
                    success = True
            else:
                log("rootfs.squashfs not found after download")
                success = False
    finally:
        _dnat_restore()
        _start_hijack()

    return success


if __name__ == "__main__":
    # Accept serial as command-line arg, or auto-detect
    serial = sys.argv[1] if len(sys.argv) > 1 else None
    ok = update(serial)
    sys.exit(0 if ok else 1)
