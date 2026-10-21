#!/usr/bin/env python3
"""💎 EARNINGS EPS (Tom lệnh 20/10; tên gốc "Gom Góp Tinh Hoa", Tom đổi sang
"Earnings EPS" cho gọn chiều 20/10) — mục tóm lược earnings trên bảng Live.

Đọc tinhhoa.json (+ zones.json là nguồn vùng), render block giữa markers
TINHHOA-AUTO trong section #sec-tinhhoa của bảng, ghi canon + index.html
(file tạm + os.replace, assert PIN gate), stamp version, commit repo,
deploy qua deploy.py (git push -> fallback API PUT + verify live).

Entry tinhhoa.json:
  PENDING: ticker, name, date (YYYY-MM-DD), when, status="PENDING",
           eps_est, rev_est, implied, note
  DONE (Cú Đêm điền sau khi có kết quả): status="DONE", eps_act, rev_act,
           verdict (BEAT|MISS|MIXED), guidance, reaction_pct, price_after, essence
💎 BÁU VẬT: entry DONE có price_after lọt vùng zones.json -> viền vàng + huy hiệu.
Tự dọn: entry quá 4 ngày (theo date) bị loại khỏi state; hiển thị tối đa 8.
Dùng: python3 tinhhoa_update.py [--no-deploy]
"""
import datetime
import json
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

DIR = os.path.dirname(os.path.abspath(__file__))
CANON = os.path.expanduser(
    "~/workspace/goals/watchlist-l-t-s-ng-h-ng-ng-y/files/bang-market-live.html")
INDEX = os.path.join(DIR, "index.html")
STATE = os.path.join(DIR, "tinhhoa.json")
ZONES = os.path.join(DIR, "zones.json")
START, END = "<!-- TINHHOA-AUTO-START -->", "<!-- TINHHOA-AUTO-END -->"
PT = ZoneInfo("America/Los_Angeles")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def zone_of(zones, t):
    z = zones.get(t)
    if not isinstance(z, dict):
        return None
    e = z.get("entry")
    if not (isinstance(e, (list, tuple)) and len(e) == 2):
        return None
    tp = z.get("targets") or z.get("tp") or []
    return {"book": z.get("book"), "lo": e[0], "hi": e[1],
            "tp": tp, "cut": z.get("cut")}


def money(x):
    return f"${x:,.2f}" if isinstance(x, (int, float)) else "?"


def is_treasure(e, zones):
    z = zone_of(zones, e.get("ticker", ""))
    pa = e.get("price_after")
    return bool(z and isinstance(pa, (int, float)) and z["lo"] <= pa <= z["hi"])


def zone_line(z):
    if not z:
        return ""
    tp = ""
    if z["tp"]:
        tp = " · Chốt " + "→".join(f"${x:g}" for x in z["tp"])
    cut = f" · Cắt <${z['cut']:g}" if isinstance(z.get("cut"), (int, float)) else ""
    return (f"<br><span style='opacity:.85'>🎯 {z.get('book') or ''}: "
            f"Vào ${z['lo']:g}–${z['hi']:g}{tp}{cut}</span>")


def render_entry(e, zones):
    t = e.get("ticker", "?")
    zl = zone_line(zone_of(zones, t))
    when = e.get("when", "")
    if e.get("status") == "DONE":
        v = {"BEAT": "✅ BEAT", "MISS": "❌ MISS",
             "MIXED": "➖ MIXED"}.get(e.get("verdict"), e.get("verdict") or "")
        treasure = ""
        style = ""
        if is_treasure(e, zones):
            treasure = (" <span style='background:linear-gradient(90deg,#d4af37,#f5d76e);"
                        "color:#111;font-weight:800;padding:1px 7px;border-radius:8px'>"
                        "💎 BÁU VẬT — RƠI VÀO VÙNG!</span>")
            style = "border:2px solid #d4af37;box-shadow:0 0 10px rgba(212,175,55,.45);"
        bits = []
        if e.get("eps_act") is not None or e.get("eps_est") is not None:
            bits.append(f"EPS {money(e.get('eps_act'))} vs kỳ vọng {money(e.get('eps_est'))}")
        if e.get("rev_act") or e.get("rev_est"):
            bits.append(f"Doanh thu {e.get('rev_act') or '?'} vs {e.get('rev_est') or '?'}")
        g = f"<br>🧭 Guidance: {e['guidance']}" if e.get("guidance") else ""
        rl = ""
        r = e.get("reaction_pct")
        pa = e.get("price_after")
        if isinstance(r, (int, float)):
            rl = f"<br>📈📉 Phản ứng sau báo cáo: {r:+.1f}%"
            if isinstance(pa, (int, float)):
                rl += f" → giá ~${pa:,.2f}"
        elif isinstance(r, str) and r.strip():
            # chuỗi đã gồm giá/giờ (vd lượt late-update) → render nguyên văn
            rl = f"<br>📈📉 Phản ứng sau báo cáo: {r}"
        es = f"<br>✨ <b>Tinh hoa:</b> {e['essence']}" if e.get("essence") else ""
        return (f"<div class='alert' style='{style}padding:8px 10px;margin:6px 0;"
                f"word-break:break-word'><b>{t}</b> — {when} · {v}{treasure}"
                f"<br>{' · '.join(bits)}{g}{rl}{es}{zl}</div>")
    bits = []
    if e.get("eps_est") is not None:
        bits.append(f"EPS kỳ vọng {money(e['eps_est'])}")
    if e.get("rev_est"):
        bits.append(f"doanh thu {e['rev_est']}")
    impl = f" · biên options {e['implied']}" if e.get("implied") else ""
    nt = f"<br><span style='opacity:.8'>{e['note']}</span>" if e.get("note") else ""
    return (f"<div class='alert' style='padding:8px 10px;margin:6px 0;word-break:break-word'>"
            f"<b>{t}</b> — {when} · ⏳ CHỜ KẾT QUẢ"
            f"<br>{' · '.join(bits)}{impl}{nt}{zl}</div>")


def ensure_section(h):
    if 'id="sec-tinhhoa"' not in h:
        nav_old = '<a href="#sec-urgent" class="tnav shark">🦈 CÁ MẬP</a>'
        if nav_old in h:
            h = h.replace(nav_old, nav_old +
                          ' <a href="#sec-tinhhoa" class="tnav">💎 EARNINGS EPS</a>', 1)
        skel = ('<div class="sec" id="sec-tinhhoa">\n'
                '  <h2>💎 EARNINGS EPS — tóm lược sau báo cáo, săn báu vật rơi vào vùng</h2>\n'
                '  <div class="note" style="margin-bottom:8px">Sau mỗi kỳ báo cáo: số thật vs kỳ vọng'
                ' • guidance • phản ứng giá • giá rơi vào vùng gom đã duyệt = 💎 BÁU VẬT.'
                ' Cú Đêm tự điền số thật sau giờ đóng cửa. Vùng giá chỉ là tham chiếu kỹ thuật,'
                ' không phải lệnh mua/bán.</div>\n'
                f'  {START}{END}\n</div>\n\n')
        anchor = '<div class="sec" id="sec-sancm">'
        assert anchor in h, "không thấy sec-sancm để chèn trước"
        h = h.replace(anchor, skel + anchor, 1)
    return h


def main():
    deploy = "--no-deploy" not in sys.argv
    today = datetime.datetime.now(PT).date()
    state = load(STATE, {"entries": []})
    zones = load(ZONES, {})
    kept = []
    for e in state.get("entries", []):
        try:
            d = datetime.date.fromisoformat(e.get("date", ""))
        except Exception:
            continue
        if (today - d).days <= 4:
            kept.append(e)
    def rank(e):
        if e.get("status") == "DONE" and is_treasure(e, zones):
            return (0, e.get("date", ""))
        if e.get("status") == "PENDING":
            return (1, e.get("date", ""))
        return (2, "")  # DONE thường: mới nhất trước (nhờ pre-sort đảo + sort ổn định)
    kept.sort(key=lambda e: e.get("date", ""), reverse=True)  # pre-sort: mới nhất trước
    kept.sort(key=rank)  # sort ổn định: DONE thường giữ thứ tự mới-nhất-trước
    kept = kept[:8]
    state["entries"] = kept
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)

    with open(CANON) as f:
        h = f.read()
    h = ensure_section(h)
    assert START in h and END in h, "thiếu marker TINHHOA"
    block = "\n" + "\n".join(render_entry(e, zones) for e in kept) + "\n  "
    i, j = h.index(START) + len(START), h.index(END)
    h = h[:i] + block + h[j:]
    assert "PIN-GATE-START" in h, "MẤT PIN GATE — dừng"
    sys.path.insert(0, DIR)
    from quick_refresh import stamp_version
    h, v = stamp_version(h, DIR)
    for path in (CANON, INDEX):
        t = path + ".tmp"
        with open(t, "w") as f:
            f.write(h)
        os.replace(t, path)
    bal = h.count("<div") - h.count("</div>")
    print(f"tinhhoa: {len(kept)} entries, stamp v={v}, div lệch={bal}")
    if deploy:
        subprocess.run(["git", "add", "index.html", "version.json"], cwd=DIR,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", f"tinhhoa update v={v}"],
                       cwd=DIR, capture_output=True)
        r = subprocess.run([sys.executable, os.path.join(DIR, "deploy.py")],
                           cwd=DIR, capture_output=True, text=True, timeout=600)
        print("deploy rc:", r.returncode)
        print((r.stdout or "")[-600:])
        print((r.stderr or "")[-300:])


if __name__ == "__main__":
    main()
