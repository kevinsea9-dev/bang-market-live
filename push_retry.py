#!/usr/bin/env python3
"""🔄 PUSH RETRY — Tom 16/10: "hệ thống không tự động update, xài dỏm quá".

Từ 19/10/2026: toàn bộ logic đẩy nằm ở deploy.py (2 đường truyền git + API fallback,
tự kiểm chứng bằng live version.json, flock chống chạy chồng). File này chỉ là vỏ gọi
deploy.py — KHÔNG commit gì trong working tree."""
import os, subprocess, sys

REPO = os.path.expanduser("~/workspace/market-board")

if __name__ == "__main__":
    r = subprocess.run([sys.executable, os.path.join(REPO, "deploy.py")],
                       cwd=REPO, timeout=1500)
    sys.exit(r.returncode)
