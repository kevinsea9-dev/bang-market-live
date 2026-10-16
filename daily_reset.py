#!/usr/bin/env python3
"""BOARD DAILY RESET — chạy 03:37 mỗi ngày giao dịch, trước radar 04:00.
Dọn bảng cho ngày mới, đúng chất Live:
1. Xóa tín hiệu vùng của hôm qua (giữ header, làm trống danh sách).
2. Archive các mục hunt cũ hơn 3 ngày vào hidden_files, xóa khỏi bảng.
3. Không đụng giá/vùng/zones.json.
Tom lệnh 14/10/2026: bảng Live phải tươi mỗi ngày, không xem lại tin cũ.
"""
import argparse
import datetime
import json
import os
import re
import sys

ZONE = datetime.timezone(datetime.timedelta(hours=-7))  # PT (PDT)


def today_pt():
    return datetime.datetime.now(ZONE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True)
    ap.add_argument("--hidden-dir", required=True)
    a = ap.parse_args()

    now = today_pt()
    today = now.strftime("%d/%m")
    h = open(a.html, encoding="utf-8").read()
    actions = []

    # 1. Xóa tín hiệu vùng KHÔNG phải hôm nay — ngày mới, tín hiệu mới
    #    (giữ lại tín hiệu của hôm nay nếu script chạy muộn)
    #    FIX 15/10: match div.alert CÓ class (alert hot/good) chứ không chỉ div.alert trơn
    def sig_old(m):
        block = m.group(0)
        keep = re.findall(r'<div class="alert[^"]*">(.*?)</div>', block, flags=re.S)
        kept = [a for a in keep if today in a]
        if kept:
            return (m.group(1) + "\n" +
                    "".join(f'<div class="alert">{a}</div>\n' for a in kept))
        actions.append(f"xóa {len(keep)} tín hiệu vùng cũ")
        return m.group(1) + "\n"

    h = re.sub(
        r'(<h2>🚨 TÍN HIỆU VÙNG — mới nhất lên đầu</h2>)\s*((?:<div class="alert[^"]*">.*?</div>\s*)+)',
        sig_old, h, flags=re.S)

    # 1b. Xóa tín hiệu DRAM cũ (không phải hôm nay) trong mục DRAM
    def dram_sig_old(m):
        block = m.group(0)
        keep = re.findall(r'<div class="alert[^"]*">(.*?)</div>', block, flags=re.S)
        kept = [a for a in keep if today in a]
        if kept:
            return (m.group(1) + "\n" +
                    "".join(f'<div class="alert">{a}</div>\n' for a in kept))
        if keep:
            actions.append(f"xóa {len(keep)} tín hiệu DRAM cũ")
        return m.group(1) + "\n"

    h = re.sub(
        r'(id="sec-dram">\s*<h2>.*?</h2>)\s*((?:<div class="alert[^"]*">.*?</div>\s*)+)',
        dram_sig_old, h, flags=re.S)

    # 1c. Dọn TIN LỚN + MÃ NÓNG + ĐÁNG CHÚ Ý: chỉ giữ tin hôm nay và entry Cú Đêm
    #     (FIX 15/10: Tom lệnh "tin cũ phải biến mất" — trước đây không dọn các mục này)
    def news_old(m):
        block = m.group(0)
        alerts = re.findall(r'(<div class="alert[^"]*">.*?</div>)\s*', block, flags=re.S)
        kept = [a for a in alerts if today in a or 'Cú Đêm' in a or 'CÚ ĐÊM' in a]
        if kept:
            actions.append(f"dọn {m.group(1)[:30]}: giữ {len(kept)}/{len(alerts)}")
            return m.group(1) + "\n" + "\n".join(kept) + "\n"
        if alerts:
            actions.append(f"xóa {len(alerts)} tin cũ {m.group(1)[:30]}")
        return m.group(1) + "\n"

    for h2pat in [r'📰 TIN LỚN', r'🔥 MÃ NÓNG HÔM NAY', r'🔔 ĐÁNG CHÚ Ý NHẤT']:
        h = re.sub(
            r'(<h2>' + h2pat + r'.*?</h2>)\s*((?:<div class="alert[^"]*">.*?</div>\s*)+)',
            news_old, h, flags=re.S)

    # 2. Archive mục hunt cũ hơn 3 ngày: <h2>🎯 HUNT ... (DD/MM) ...
    def hunt_old(m):
        d, mo = int(m.group(1)), int(m.group(2))
        try:
            hunt_date = datetime.date(now.year, mo, d)
        except ValueError:
            return m.group(0)
        age = (now.date() - hunt_date).days
        if age > 3:
            arc = os.path.join(a.hidden_dir,
                               f"archived-hunt-{d:02d}-{mo:02d}.html")
            open(arc, "w", encoding="utf-8").write(m.group(0))
            actions.append(f"archive mục hunt {d:02d}/{mo:02d} ({age} ngày)")
            return ""
        return m.group(0)

    h = re.sub(
        r'<div class="sec">\s*<h2>🎯 HUNT[^<]*\((\d{2})/(\d{2})\)[^<]*</h2>.*?</table>\s*</div>',
        hunt_old, h, flags=re.S)

    # 2b. Dọn MỌI section có ngày trong h2 mà cũ từ 2 ngày trở lên (FIX 16/10:
    #     Tom phàn nàn section DRAM 14/10, HUNT 13/10 nằm chình ình vì chỉ dọn
    #     hunt >3 ngày. Từ giờ: section nào có (DD/MM) trong h2 mà >=2 ngày là archive + xóa.)
    def sec_date_old(h2txt):
        m = re.search(r'\((\d{1,2})/(\d{1,2})\)', h2txt)
        if not m:
            return None
        try:
            sec_date = datetime.date(now.year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        return (now.date() - sec_date).days

    parts = re.split(r'(<div class="sec">)', h)
    out = [parts[0]]
    i = 1
    while i < len(parts):
        tag, body = parts[i], parts[i + 1] if i + 1 < len(parts) else ''
        block = tag + body
        m2 = re.search(r'<h2>([^<]*)</h2>', block)
        age = sec_date_old(m2.group(1)) if m2 else None
        if age is not None and age >= 2:
            arc = os.path.join(
                a.hidden_dir,
                f"archived-sec-{now.strftime('%m%d')}-{re.sub(r'[^A-Za-z0-9]+', '-', m2.group(1)[:20]).strip('-')}.html")
            open(arc, "w", encoding="utf-8").write(block)
            actions.append(f"archive section cũ '{m2.group(1)[:30]}' ({age} ngày)")
        else:
            out.append(block)
        i += 2
    h = ''.join(out)

    # 3. Header ngày của cột giá theo đúng hôm nay
    h = re.sub(r"<th>Giá \d{2}/\d{2}</th>", f"<th>Giá {today}</th>", h)

    # validate cơ bản
    if "PIN-GATE-START" not in h or "<!-- PIN-GATE-END -->" not in h:
        print("VALIDATE FAIL: mất PIN gate", file=sys.stderr)
        return 2
    if h.count("<div") != h.count("</div>"):
        print("VALIDATE FAIL: lệch thẻ div", file=sys.stderr)
        return 2

    open(a.html, "w", encoding="utf-8").write(h)
    print("daily reset: " + ("; ".join(actions) if actions else "không có gì cũ để dọn"),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
