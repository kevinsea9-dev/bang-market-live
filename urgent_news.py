#!/usr/bin/env python3
"""URGENT NEWS — Tom lệnh 15/10/2026: tin quan trọng phải có tín hiệu báo khẩn cấp HOT.
Chèn alert tin khẩn (chớp đỏ) vào #urgentBox đầu bảng. Dedup theo hash tiêu đề.
Dùng bởi news-klaxon-intraday cron sau khi worker xác minh tin market-moving.
"""
import argparse, hashlib, os, re, sys


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--html", required=True)
    a.add_argument("--repo-dir", required=True)
    a.add_argument("--title", required=True)
    a.add_argument("--body", required=True)
    a.add_argument("--source", default="")
    a = a.parse_args()

    h = open(a.html, encoding="utf-8").read()
    # PIN gate đã tắt theo lệnh Tom 16/10
    if "PIN-GATE-OFF" not in h:
        print("PIN-GATE-OFF MISSING — không push", file=sys.stderr)
        return 2
    digest = hashlib.md5(a.title.encode()).hexdigest()[:10]
    mark = f"<!-- URG-NEWS-{digest} -->"
    if mark in h:
        print("tin đã có trên bảng — bỏ qua (dedup)")
        return 0
    src = f" • {a.source}" if a.source else ""
    alert = (f'{mark}<div class="alert urgent"><b>🚨 {a.title}</b><br>{a.body}<br>'
             f'<span class="t">Tin khẩn market-moving{src} • chỉ tham khảo</span></div>\n')
    h2, c = re.subn(r'(<div id="urgentBox">)<div class="note">Chưa có tín hiệu khẩn.*?</div>',
                    r"\g<1>" + alert, h, count=1, flags=re.S)
    if not c:
        h2, c = re.subn(r'(<div id="urgentBox">)', r"\g<1>" + alert, h, count=1)
    if not c:
        print("không thấy #urgentBox", file=sys.stderr)
        return 2
    h = h2
    open(a.html, "w", encoding="utf-8").write(h)
    repo_html = os.path.join(a.repo_dir, "index.html")
    if os.path.abspath(a.html) != os.path.abspath(repo_html):
        open(repo_html, "w", encoding="utf-8").write(h)
    cwd = os.getcwd()
    os.chdir(a.repo_dir)
    res = "failed"
    for _ in range(3):
        os.system("git add index.html >/dev/null 2>&1")
        os.system('git -c user.email="tom@local" -c user.name="Tom" commit -qm "tin khan" >/dev/null 2>&1')
        os.system("git pull --rebase origin main >/dev/null 2>&1")
        if os.system("git push origin main >/dev/null 2>&1") == 0:
            res = "pushed"
            break
    os.chdir(cwd)
    print(f"tin khẩn đã lên bảng | git: {res}", flush=True)
    return 0 if res in ("pushed", "nothing-to-commit") else 3


if __name__ == "__main__":
    sys.exit(main())
