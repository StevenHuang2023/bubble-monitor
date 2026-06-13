#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地预览服务器（可选）：托管静态文件 + /refresh 实时重新取数。

  python3 serve.py   →  打开 http://localhost:8753

GitHub Pages 上用不到它：线上由 GitHub Actions 定时跑 fetch.py 更新 data.json，
网页自身每隔几分钟自动重载 data.json。这个本地服务器只是给你本机“立即重拉”用。
"""
import http.server, importlib, os, socketserver

PORT = 8753
ROOT = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/refresh":
            try:
                import fetch
                importlib.reload(fetch)
                fetch.main()
                body = open(os.path.join(ROOT, "data.json"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
            return
        super().do_GET()

if __name__ == "__main__":
    os.chdir(ROOT)
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"看板: http://localhost:{PORT}   (Ctrl+C 退出)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出")
