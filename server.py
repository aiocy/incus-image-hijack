#!/usr/bin/env python3
"""
Incus Image Hijack Server
HTTPS server that intercepts requests to blocked incus image mirrors
and serves pre-downloaded image files locally.
"""
import http.server
import ssl
import sys
import os

PORT = int(os.environ.get("HIJACK_PORT", "443"))
ROOT = os.environ.get("HIJACK_ROOT", "/opt/image-hijack")
CERT = os.environ.get("HIJACK_CERT", "/opt/image-hijack/server.pem")
KEY = os.environ.get("HIJACK_KEY", "/opt/image-hijack/server.key")


class HijackHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[HIJACK] %s %s %s\n" % args)
        sys.stderr.flush()


def main():
    httpd = http.server.HTTPServer(("127.0.0.1", PORT), HijackHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print("[HIJACK] Running on :%d" % PORT, flush=True)
    print("[HIJACK] Serving from %s" % ROOT, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[HIJACK] Shutting down", flush=True)
        httpd.shutdown()


if __name__ == "__main__":
    main()
