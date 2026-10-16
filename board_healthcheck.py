#!/usr/bin/env python3
"""BOARD HEALTHCHECK — Tom lệnh 16/10 (~09:35):
Hệ thống tự động kiểm tra bảng Live, KHÔNG để tái diễn vấn đề trùng lặp / ứ đọng.
Chạy mỗi 15 phút trong phiên (cron riêng). Tự sửa được thì sửa, không sửa được thì báo.

KIỂM TRA:
1. Urgent alert: 1 mã = 1 alert (theo data-ticker), giữ mới nhất
2. Shark-of-day: không mã nào lặp 2 lần
3. Tin tức: tối đa 30 tin, không trùng nội dung
4. Alert/tin cũ (không phải hôm nay): xóa
5. Mã trong _excluded không được xuất hiện lại
6. HTML hợp lệ: div cân bằng, còn marker PIN-GATE-OFF
7. version.json tươi (trong phiên phải <20 phút)

Tự sửa + commit + push nếu có thay đổi. In HEALTHCHECK: OK hoặc liệt kê vấn đề.
"""
import re, json, os, sys, subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone

PT = timezone(timedelta(hours=-7))
REPO = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(REPO, "index.html")
CANON = os.path.expanduser("~/workspace/goals/watchlist-l-t-s-ng-h-ng-ng-y/files/bang-market-live.html")

def now_pt():
    return datetime.now(PT)

def git(cmd):
    r = subprocess.run(["git", "-C", REPO] + cmd, capture_output=True, text=True, timeout=60)
    return r.stdout.strip()

def main():
    issues, fixed = [], []
    h = open(CANON).read()
    today = now_pt().strftime("%d/%m")

    # 1. Urgent: 1 mã 1 alert (giữ mới nhất theo data-ts)
    alerts = list(re.finditer(
        r'<!-- ([A-Z-]+) --><div class="alert urgent" data-ticker="([A-Z]{2,6})"[^>]*data-ts="([^"]*)"[^>]*>.*?</div>|'
        r'<div class="alert urgent" data-ticker="([A-Z]{2,6})"[^>]*data-ts="([^"]*)"[^>]*>.*?</div>',
        h, re.S))
    by_ticker = {}
    for m in alerts:
        t = m.group(2) or m.group(4)
        ts = m.group(3) or m.group(5) or ""
        by_ticker.setdefault(t, []).append((ts, m.group(0)))
    for t, lst in by_ticker.items():
        if len(lst) > 1:
            issues.append(f"trùng alert {t} x{len(lst)}")
            lst.sort(key=lambda x: x[0])
            for _, old_html in lst[:-1]:  # giữ mới nhất
                h = h.replace(old_html, "", 1)
            fixed.append(f"gộp {t} còn 1 alert")

    # 2. Shark-of-day: mã không lặp
    m = re.search(r'(<div class="sharkday".*?</div>\s*</div>)', h, re.S)
    if m:
        sd = m.group(1)
        rows = re.findall(r'<div class="sd-row">.*?</div>', sd, re.S)
        seen, new_rows, dup = set(), [], False
        for r in rows:
            mt = re.search(r'<b>([A-Z]{2,6})</b>', r)
            t = mt.group(1) if mt else ""
            if t and t in seen:
                dup = True
                issues.append(f"sharkday trùng {t}")
                continue
            seen.add(t)
            new_rows.append(r)
        if dup:
            h = h.replace(sd, sd.replace("".join(rows), "".join(new_rows)))
            fixed.append("sharkday hết trùng")

    # 3. Tin tức: tối đa 30, không trùng
    ni = h.find('id="sec-news"')
    if ni != -1:
        nj = h.find('</div>', h.find('</ul>', ni))
        sec = h[ni:nj]
        lis = list(re.finditer(r'<li[^>]*>.*?</li>', sec, re.S))
        if len(lis) > 30:
            issues.append(f"tin ứ đọng {len(lis)}")
            for mm in reversed(lis[30:]):
                sec = sec[:mm.start()] + sec[mm.end():]
            fixed.append(f"cắt tin còn 30 (xóa {len(lis)-30})")
        # trùng nội dung
        seen_c, dups = set(), 0
        def dedup_li(mm):
            nonlocal dups
            txt = re.sub(r'<[^>]+>', '', mm.group(0)).strip()[:70].lower()
            txt = re.sub(r'\s+', ' ', txt)
            if txt in seen_c:
                dups += 1
                return ""
            seen_c.add(txt)
            return mm.group(0)
        sec2 = re.sub(r'<li[^>]*>.*?</li>', dedup_li, sec, flags=re.S)
        if dups:
            issues.append(f"tin trùng nội dung x{dups}")
            fixed.append(f"xóa {dups} tin trùng")
        h = h[:ni] + sec2 + h[nj:]

    # 4. Alert/tin cũ không phải hôm nay
    def old_alert(mm):
        inner = mm.group(1)
        if 'Cú Đêm' in inner or today in inner:
            return mm.group(0)
        if re.search(r'\d{1,2}/\d{1,2}', inner):
            return ""
        return mm.group(0)
    h2 = re.sub(r'<div class="alert[^"]*">(.*?)</div>', old_alert, h, flags=re.S)
    n_old = h.count('<div class="alert') - h2.count('<div class="alert')
    if n_old:
        issues.append(f"alert cũ x{n_old}")
        fixed.append(f"xóa {n_old} alert cũ")
        h = h2

    # 5. Mã excluded không được quay lại
    try:
        excluded = json.load(open(os.path.join(REPO, "zones.json"))).get("_excluded", [])
        back = [t for t in excluded if re.search(r'<b>' + re.escape(t) + r'</b>', h)]
        if back:
            issues.append(f"mã excluded quay lại: {back} — NGUY HIỂM, cần xem tay")
    except Exception as e:
        issues.append(f"không đọc được zones.json: {e}")

    # 5b. Tom 16/10: không còn nút chết — mọi href="#sec-*" phải có section tương ứng
    links = set(re.findall(r'href="#(sec-[a-z]+)"', h))
    ids = set(re.findall(r'id="(sec-[a-z]+)"', h))
    dead = links - ids
    if dead:
        issues.append(f"nút chết: {sorted(dead)} — bấm không đi đâu")

    # 5c. Tom 16/10: không trùng id (VIX từng bị 2 thẻ)
    for _id in ["vix-gauge", "sysCheck"]:
        _c = h.count(f'id="{_id}"')
        if _c > 1:
            issues.append(f"trùng id {_id} x{_c}")
            h = re.sub(f'<div[^>]*id="{_id}">.*?</div>', "", h, count=_c-1, flags=re.S)

    # 6. HTML hợp lệ
    if h.count('<div') != h.count('</div>'):
        issues.append("HTML vỡ div — NGUY HIỂM, cần xem tay")
    pin_on = 'PIN-GATE-START' in h and 'BOARD_PIN' in h
    pin_off = 'PIN-GATE-OFF' in h
    if not (pin_on or pin_off):
        issues.append("mất PIN guard (cả gate lẫn marker OFF) — NGUY HIỂM")

    # 7. version.json tươi (trong phiên 06:30-13:00)
    try:
        v = json.load(open(os.path.join(REPO, "version.json")))["v"]
        vdt = datetime.strptime(v, "%Y%m%d%H%M%S").replace(tzinfo=PT)
        age = (now_pt() - vdt).total_seconds() / 60
        wd = now_pt().weekday()
        hm = now_pt().hour * 60 + now_pt().minute
        in_session = wd < 5 and 390 <= hm <= 780
        if in_session and age > 20:
            issues.append(f"version cũ {age:.0f} phút — refresh 5m có thể kẹt")
    except Exception as e:
        issues.append(f"version.json lỗi: {e}")

    if fixed:
        open(CANON, "w").write(h)
        open(HTML, "w").write(h)
        subprocess.run(["git", "-C", REPO, "add", "index.html"], timeout=30)
        # version bump
        v = {"v": now_pt().strftime("%Y%m%d%H%M%S")}
        json.dump(v, open(os.path.join(REPO, "version.json"), "w"))
        subprocess.run(["git", "-C", REPO, "add", "version.json"], timeout=30)
        subprocess.run(["git", "-C", REPO, "-c", "user.name=Iggy", "-c", "user.email=iggy@local",
                        "commit", "-m", "healthcheck: " + ", ".join(fixed)], timeout=30)
        p = subprocess.run(["git", "-C", REPO, "push", "origin", "main"],
                           capture_output=True, text=True, timeout=90)
        if p.returncode == 0:
            fixed.append("đã push")
        else:
            issues.append("push thất bại — cần xem tay")

    if issues:
        print("HEALTHCHECK ISSUES: " + " | ".join(issues))
    if fixed:
        print("HEALTHCHECK FIXED: " + " | ".join(fixed))
    if not issues and not fixed:
        print("HEALTHCHECK: OK ✓")

    # Tom 16/10: gắn ĐÈN BÁO trạng thái lên bảng để nhìn thấy hệ thống đang chạy
    try:
        now_s = now_pt().strftime("%d/%m %H:%M")
        if any("NGUY HIỂM" in i for i in issues):
            badge = f'<div id="sysCheck" style="text-align:center;font-size:12px;color:#f87171;padding:4px">🔴 Hệ thống tự kiểm tra: CÓ VẤN ĐỀ ({now_s}) — đang xử lý</div>'
        elif fixed:
            badge = f'<div id="sysCheck" style="text-align:center;font-size:12px;color:#fbbf24;padding:4px">🟡 Hệ thống tự kiểm tra: đã tự sửa {len(fixed)} mục ({now_s})</div>'
        else:
            badge = f'<div id="sysCheck" style="text-align:center;font-size:12px;color:#4ade80;padding:4px">🟢 Hệ thống tự kiểm tra: bảng sạch ({now_s})</div>'
        if 'id="sysCheck"' in h:
            h = re.sub(r'<div id="sysCheck".*?</div>', badge, h, flags=re.S)
        else:
            # chèn ngay sau badge Cập nhật lần cuối
            h = h.replace('id="lastUpdate"', 'id="lastUpdate"', 1)
            m2 = re.search(r'(Cập nhật lần cuối[^<]*</[^>]+>)', h)
            if m2:
                h = h.replace(m2.group(1), m2.group(1) + "\n" + badge, 1)
        open(CANON, "w").write(h)
        open(HTML, "w").write(h)
        print("đèn báo trạng thái đã gắn lên bảng")
    except Exception as e:
        print(f"! gắn đèn báo: {e}", file=sys.stderr)

    return 0 if not any("NGUY HIỂM" in i for i in issues) else 1

if __name__ == "__main__":
    sys.exit(main())
