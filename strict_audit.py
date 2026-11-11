#!/usr/bin/env python3
"""STRICT AUDIT mẫu khối thống nhất (Tom bắt lỗi lần 2 tối 5/11).

Khác census cũ (chỉ đếm SỰ HIỆN DIỆN của header/chân — đo 0 lỗi trong
khi Tom thấy lỗi thật trên điện thoại): script này soi NỘI DUNG từng
khối mã trong 8 mục alert:
- header zchip phải chứa đủ "Vùng" + "Chốt" + "Cắt" (bộ 3); ngoại lệ
  duy nhất: header khai rõ "Chưa có vùng duyệt" (mã watch-only chưa có
  vùng — đạt chuẩn khi NÓI RÕ, lỗi là khi có vùng mà rơi mất số).
  Từ 11/11 nhãn "Chốt" đổi thành "Bán · Target" ("Bán $X · Target
  $X→$Y") nên "Bán"+"Target" được tính đủ chân bán/target như "Chốt".
- khối phải có dòng xe (.levcomp) hoặc dòng .noveh,
- khối phải có chân .blkfoot.
Khối không có data-achip = khối prose/tin, bỏ qua — NGOẠI TRỪ sec-special:
luật Tom 5/11 21:36, mục ĐẶC BIỆT không có khối prose giới thiệu dài
(chỉ khối mã + divider DỰ BỊ); khối prose >400 ký tự text trong
sec-special = lỗi INTRO-BLOCK (board_healthcheck tự gỡ).

Dùng: python3 strict_audit.py <file.html>
In thêm dòng máy đọc được: STRICT-DEFECT: <mục> <mã>: <lỗi> và
STRICT-TOTAL: <n> (board_healthcheck.py bước 0c dùng để tự chữa + báo).
"""
import re
import sys

h = open(sys.argv[1], encoding='utf-8', errors='ignore').read()


def section(seg_id):
    m = re.search(r'<div class="sec" id="%s"' % seg_id, h)
    if not m:
        return ''
    nxt = h.find('<div class="sec" id="sec-', m.start() + 10)
    return h[m.start(): nxt if nxt > 0 else len(h)]


def top_blocks(seg):
    # split into top-level <div class="alert...> blocks by div depth
    blocks, out = [], []
    starts = [mm.start() for mm in re.finditer(r'<div class="alert', seg)]
    for s in starts:
        depth, j = 0, s
        while j < len(seg):
            if seg.startswith('<div', j):
                depth += 1
                j += 4
            elif seg.startswith('</div>', j):
                depth -= 1
                j += 6
                if depth == 0:
                    break
            else:
                j += 1
        out.append(seg[s:j])
    # dedupe nested starts (a start inside a captured block)
    res, covered = [], -1
    for s, b in zip(starts, out):
        if s > covered:
            res.append(b)
            covered = s + len(b)
    return res


ALERT_SECS = ['sec-special', 'sec-watch', 'sec-daytrade', 'sec-swing',
              'sec-hot', 'sec-urgent', 'sec-ahshark', 'sec-tickets']
total = 0
for sid in ALERT_SECS:
    seg = section(sid)
    if not seg:
        print(f"{sid}: SECTION MISSING")
        continue
    blks = top_blocks(seg)
    defects = []
    for b in blks:
        am = re.search(r'data-achip="([^"]+)"', b)
        if not am:
            # Luật Tom 5/11 21:36: sec-special không có khối prose giới
            # thiệu dài (khối "CẤU TRÚC MỚI..." đầu mục) — chỉ khối mã
            # và divider DỰ BỊ. Prose dài ở đây = INTRO-BLOCK.
            if sid == 'sec-special':
                txt = re.sub(r'\s+', ' ',
                             re.sub(r'<[^>]+>', ' ', b)).strip()
                # divider DỰ BỊ mở đầu bằng "🔹 DỰ BỊ —" (40 ký tự đầu);
                # khối giới thiệu nhắc "DỰ BỊ" ở giữa văn nên không thoát.
                if len(txt) > 400 and 'DỰ BỊ' not in txt[:40]:
                    defects.append('PROSE: INTRO-BLOCK')
            continue  # prose block, not a ticker block
        t = am.group(1)
        hdr = re.search(
            r'<div class="zchip[^"]*" data-achip="%s">(.*?)</div>' % t,
            b, re.S)
        hdr_txt = hdr.group(1) if hdr else ''
        issues = []
        if not hdr:
            issues.append('NO-HEADER')
        elif 'Chưa có vùng duyệt' in hdr_txt:
            # Mã watch-only khai rõ "⚪ Chưa có vùng duyệt" trong header:
            # đạt chuẩn theo luật earnings/bảng của Tom (không vùng thì
            # phải NÓI RÕ, không được im lặng trống số). Lỗi thật là
            # header có vùng mà rơi mất số (nhánh TAM VO HIEU cũ).
            pass
        else:
            if 'Vùng' not in hdr_txt:
                issues.append('HDR-NO-VUNG')
            # 11/11: nhãn "Chốt" đổi thành "Bán · Target" — chấp nhận
            # "Bán"+"Target" đủ chân bán/target (vá gốc vụ HDR-NO-CHOT
            # báo oan 6 mã sec-special ngày 11/11).
            if 'Chốt' not in hdr_txt and not (
                    'Bán' in hdr_txt and 'Target' in hdr_txt):
                issues.append('HDR-NO-CHOT')
            if 'Cắt' not in hdr_txt:
                issues.append('HDR-NO-CAT')
        if 'levcomp' not in b and 'noveh' not in b:
            issues.append('NO-VEH-LINE')
        if 'blkfoot' not in b:
            issues.append('NO-FOOT')
        if issues:
            defects.append(f"{t}: {'+'.join(issues)}")
    print(f"{sid}: {len(blks)} blocks | defects: {len(defects)}")
    for d in defects:
        print("   ", d)
        print(f"STRICT-DEFECT: {sid} {d}")
    total += len(defects)
print(f"STRICT-TOTAL: {total}")
sys.exit(1 if total else 0)
