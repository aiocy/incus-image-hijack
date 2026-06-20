#!/usr/bin/env python3
"""
Incus Image Hijack Server — with auto-download on cache miss.

When Incus requests an image file that's not cached:
1. Triggers auto_update.py to download and SSH-patch the new image
2. Waits for it to complete
3. Serves the patched file

Static files (CA certs, etc.) are served normally.
"""

import http.server
import os
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

PORT = 443
ROOT = "/opt/image-hijack"
AUTO_UPDATE = "/opt/image-hijack/auto_update.py"

# Track which serials are being processed to avoid duplicate downloads
_processing = set()
_processing_lock = threading.Lock()


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *a):
        sys.stderr.write(f"[HIJACK] {' '.join(str(x) for x in a)}\n")
        sys.stderr.flush()

    def do_GET(self):
        path = self.path.split("?")[0]
        local = self.translate_path(path)

        # Cached → serve immediately
        if os.path.isfile(local):
            return super().do_GET()

        # Image path pattern → auto-download
        # e.g. /images/alpine/3.21/amd64/cloud/20260619_13:00/rootfs.squashfs
        parts = path.strip("/").split("/")
        if (
            len(parts) >= 6
            and parts[0] == "images"
            and parts[-1] in ("incus.tar.xz", "rootfs.squashfs", "meta.tar.xz")
        ):
            serial = parts[5] if parts[0] == "images" else None
            if serial:
                _log(f"AUTO: {path}")
                # Wait for auto-update to complete (dedup by serial)
                with _processing_lock:
                    if serial in _processing:
                        _log(f"  already processing serial {serial}, waiting...")
                    else:
                        _processing.add(serial)

                try:
                    if serial in _processing:
                        # Run auto-update
                        _log(f"  running auto_update.py for {serial}")
                        r = subprocess.run(
                            [sys.executable, AUTO_UPDATE, serial],
                            capture_output=True, text=True, timeout=600,
                        )
                        for line in r.stdout.splitlines():
                            _log(f"  {line}")
                        if r.stderr:
                            for line in r.stderr.splitlines():
                                _log(f"  ! {line}")
                        if r.returncode != 0:
                            _log(f"  auto_update failed (exit={r.returncode})")

                    # Now serve (or 404 if still missing)
                    if os.path.isfile(local):
                        _log(f"SERVE (auto) {path}")
                        return super().do_GET()
                    self.send_error(502, "Auto-download failed")
                except subprocess.TimeoutExpired:
                    _log(f"  auto_update timed out for {serial}")
                    self.send_error(504, "Auto-download timed out")
                except Exception as e:
                    _log(f"  auto_update error: {e}")
                    self.send_error(500, str(e))
                finally:
                    with _processing_lock:
                        _processing.discard(serial)
                return

        # Not found
        self.send_error(404)


def _log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[HIJACK] {ts} {msg}", flush=True)


if __name__ == "__main__":
    # Ensure auto_update.py exists
    if not os.path.isfile(AUTO_UPDATE):
        _log(f"WARNING: {AUTO_UPDATE} not found — auto-download disabled")

    server = http.server.HTTPServer(("127.0.0.1", PORT), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        f"{ROOT}/server.pem",
        f"{ROOT}/server.key",
    )
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    _log(f"Running on :{PORT}")
    server.serve_forever()
