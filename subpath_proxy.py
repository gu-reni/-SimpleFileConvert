#!/usr/bin/env python3
"""模拟 nginx 把 /convert/ 转发到后端（proxy_pass 带尾斜杠 = 剥掉前缀）。

目的：在**不动 nginx** 的前提下，验证「服务挂在子路径下」这个方案到底能不能工作
—— 重点是页面的相对路径在子路径下是否还能正确解析。

  /                    → 工具索引页（和 nginx 里的静态页一致）
  /convert             → 301 到 /convert/
  /convert/xxx         → 转发到 127.0.0.1:18089/xxx

用法：.venv/bin/python subpath_proxy.py [监听端口]
"""
from __future__ import annotations

import http.client
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ("127.0.0.1", 18089)
PREFIX = "/convert"

INDEX = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>小工具 · 索引</title></head>
<body style="background:#131211;color:#EAE3D8;font-family:system-ui;padding:48px">
<h1 style="font-weight:500">小工具</h1>
<p style="color:#8f867a">自己写的小工具都挂在这里。</p>
<p><a href="/convert/" style="color:#C9A227">格式转换</a> —— 图片、文档、音视频、OCR</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):          # 静音
        pass

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _proxy(self, rest: str, method: str, body: bytes | None):
        conn = http.client.HTTPConnection(*UPSTREAM, timeout=120)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "connection", "accept-encoding")}
        headers["X-Forwarded-Proto"] = "http"          # 局域网明文场景
        headers["Host"] = self.headers.get("Host", "localhost")
        try:
            conn.request(method, rest + (("?" + self.path.split("?", 1)[1]) if "?" in self.path else ""),
                         body=body, headers=headers)
            r = conn.getresponse()
            data = r.read()
        except Exception as e:                          # noqa: BLE001
            self._send(502, f"上游错误: {e}".encode())
            return
        finally:
            conn.close()
        hop = {"connection", "keep-alive", "transfer-encoding", "content-encoding"}
        extra = {k: v for k, v in r.getheaders() if k.lower() not in hop}
        self._send(r.status, data, r.getheader("Content-Type", "application/octet-stream"), extra)

    def _handle(self, method: str):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        path = self.path

        if path == PREFIX:                              # 补斜杠（nginx: location = /convert）
            self._send(301, b"", extra={"Location": PREFIX + "/"})
        elif path.startswith(PREFIX + "/"):
            self._proxy(path[len(PREFIX):], method, body)   # 剥前缀
        elif path in ("/", "/index.html"):
            self._send(200, INDEX.encode())
        else:
            self._send(404, b"not found")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18090
    print(f"[proxy] 127.0.0.1:{port}  {PREFIX}/  →  {UPSTREAM[0]}:{UPSTREAM[1]}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
