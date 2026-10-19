#!/usr/bin/env python3
"""GAP SCAN — Tom lệnh 15/10/2026: "tiền ngon nhất ở mở cửa + premarket, xanh hay đỏ đều ăn".
Hai lượt:
  --mode premarket (~06:00 PT): quét gap premarket -> alert 🔔 PREMARKET
  --mode open      (~06:32 PT): quét gap mở cửa + đà đầu phiên -> alert 🔔 MỞ CỬA
Ngưỡng: |gap| >= 2%. Chỉ chạy trong khung giờ của mode, ngoài khung thoát im lặng.
Alert prepend vào 🚨 TÍN HIỆU VÙNG (merge, không overwrite), dedup theo ngày+mã+mode.
"""
import argparse, json, os, re, sys, datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PT = datetime.timezone(datetime.timedelta(hours=-7))
GAP_MIN = 2.0


def now_pt():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(PT)


def discover_tickers(h):
    found = re.findall(r"<tr><td><b>([A-Z]{2,5})</b></td>", h)
    # vũ trụ mở rộng (đồng bộ với NHIP_EXTRA của quick_refresh.py) —
    # Tom 15/10: miss CRM/AAP/TGTX/WMT/TGT vì chỉ quét mã trên bảng
    extra = ["SKHY", "SMMT", "AMAT", "WDC",
             "CRM", "AAP", "TGTX", "WMT", "TGT", "BA", "COIN", "AAPL",
             "MSFT", "META", "ORCL", "PLTR", "UBER", "SHOP", "DIS",
             "INTC", "AAOI", "SOXL",
             "AVAV", "KTOS", "IONQ", "OKLO",
             "LMT", "IBM", "DELL",
             "CRWD", "APLD", "BROS", "TOST", "NKE", "DDOG", "TTD",
             "SBUX", "PANW", "CRWV", "ABNB", "MSTR", "OKTA",
             "BABA", "MRNA", "UPST",
             "DLTR", "ABBV", "TMDX", "GLW", "SYM", "CIEN",
             "USAR", "MP", "LAC", "UUUU", "SMCI", "LULU", "CRML",
             "UAL", "F", "NIO", "RIVN", "ACHR"]
    return sorted(set(found) | set(extra))


def fetch_candles(ticker):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           "?interval=5m&range=1d&includePrePost=true")
    try:
        req = Request(url, headers=UA)
        with urlopen(req, timeout=12) as r:
            d = json.load(r)["chart"]["result"][0]
        meta = d["meta"]
        # prev = đóng cửa phiên trước: lấy từ nến NGÀY gần nhất.
        # meta chartPreviousClose của Yahoo có lúc STALE (sáng 19/10 trả về giá thứ Năm
        # trong khi nến ngày đã có giá thứ Sáu → gap sai cả dấu). Chỉ dùng meta làm fallback.
        prev = 0.0
        try:
            dreq = Request(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                           "?interval=1d&range=5d", headers=UA)
            with urlopen(dreq, timeout=12) as dr:
                dd = json.load(dr)["chart"]["result"][0]
            dcloses = [c for c in dd["indicators"]["quote"][0]["close"] if c is not None]
            if dcloses:
                prev = float(dcloses[-1])
        except Exception:
            pass
        if not prev:
            prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
        ts = d["timestamp"]
        q = d["indicators"]["quote"][0]
        out = []
        for i, t in enumerate(ts):
            o, c = q["open"][i], q["close"][i]
            if o is None or c is None:
                continue
            lt = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).astimezone(PT)
            out.append((lt, float(o), float(c)))
        return ticker, prev, out
    except Exception as e:
        print(f"  ! {ticker}: {e}", file=sys.stderr)
        return ticker, 0, []


def gap_pct(px, prev):
    return (px - prev) / prev * 100 if prev else 0.0


def scan(tickers, mode):
    """Trả về list dict gap."""
    rows = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        data = list(ex.map(fetch_candles, tickers))
    for t, prev, candles in data:
        if not prev or not candles:
            continue
        if mode == "premarket":
            pm = [(lt, o, c) for lt, o, c in candles
                  if lt.hour < 6 or (lt.hour == 6 and lt.minute < 30)]
            if not pm:
                continue
            last_close = pm[-1][2]
            g = gap_pct(last_close, prev)
            if abs(g) >= GAP_MIN:
                rows.append({"t": t, "gap": g, "px": last_close, "prev": prev})
        else:  # open
            reg = [(lt, o, c) for lt, o, c in candles
                   if lt.hour > 6 or (lt.hour == 6 and lt.minute >= 30)]
            if not reg:
                continue
            open_px = reg[0][1]
            cur = reg[-1][2]
            g = gap_pct(open_px, prev)
            if abs(g) >= GAP_MIN:
                mom = gap_pct(cur, open_px)
                rows.append({"t": t, "gap": g, "px": cur, "open": open_px,
                             "prev": prev, "mom": mom})
    rows.sort(key=lambda r: abs(r["gap"]), reverse=True)
    return rows


def fmt_pct(p):
    return ("+" if p > 0 else "") + f"{p:.1f}%"


def build_alert(rows, mode, n):
    stamp = n.strftime("%H:%M")
    date_s = n.strftime("%d/%m")
    if mode == "premarket":
        title = f"🔔 PREMARKET {stamp} PT — gap hình thành trước giờ mở"
        lines = []
        for r in rows[:8]:
            d = "gap lên" if r["gap"] > 0 else "gap xuống"
            lines.append(f"{r['t']} {d} {fmt_pct(r['gap'])} (~${r['px']:,.2f})")
        hint = "Gap premarket ±2%+: mở cửa dễ chạy tiếp hoặc lấp gap — canh 15 phút đầu, không đuổi."
    else:
        title = "🔔 MỞ CỬA — gap + đà đầu phiên"
        lines = []
        for r in rows[:8]:
            g, m = r["gap"], r["mom"]
            if g > 0 and m > 0:
                tag = "gap-and-go 🟢"
            elif g > 0:
                tag = "đang lấp gap 🟡"
            elif m > 0:
                tag = "hồi từ gap đỏ 🟡"
            else:
                tag = "gap-and-drop 🔴"
            lines.append(f"{r['t']} gap {fmt_pct(g)} — {tag} (đà {fmt_pct(m)})")
        hint = "Xanh hay đỏ đều có tiền: gap-and-go thì theo đà, lấp gap thì chờ cạn."
    body = "<br>".join(lines)
    return (f'<div class="alert"><b>{title}</b> — {body}<br>'
            f'<span class="t">{hint} • {date_s} • gap-scan tự động • chỉ tham khảo</span></div>\n')


def dedup_key(mode, t, date_s):
    return f"GAP-{mode}-{t}-{date_s}"


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--mode", choices=["premarket", "open"], required=True)
    a.add_argument("--html", required=True)
    a.add_argument("--repo-dir", required=True)
    a = a.parse_args()
    n = now_pt()
    if n.weekday() > 4:
        return 0
    hm = n.hour * 60 + n.minute
    if a.mode == "premarket" and not (3 * 60 + 30 <= hm < 6 * 60 + 30):
        print("ngoài khung premarket — thoát im lặng")
        return 0
    if a.mode == "open" and not (6 * 60 + 30 <= hm < 7 * 60 + 15):
        print("ngoài khung mở cửa — thoát im lặng")
        return 0

    h = open(a.html, encoding="utf-8").read()
    # PIN gate: Tom 16/10 09:52 chốt GIỮ mật khẩu như cũ — bảng phải còn gate mới được push
    # (đồng bộ với validate() trong quick_refresh.py; marker PIN-GATE-OFF là của đợt tắt nhầm sáng 16/10)
    if "PIN-GATE-START" not in h or "BOARD_PIN" not in h:
        print("PIN gate mất trên bảng — không push", file=sys.stderr)
        return 2
    tickers = discover_tickers(h)
    print(f"gap scan {a.mode}: {len(tickers)} mã...", flush=True)
    rows = scan(tickers, a.mode)
    date_s = n.strftime("%d/%m")
    # dedup: bỏ mã đã có alert gap cùng mode hôm nay
    fresh = [r for r in rows
             if dedup_key(a.mode, r["t"], date_s) not in h]
    if not fresh:
        print(f"không có gap mới (|gap|>={GAP_MIN}% hoặc đã báo hôm nay) — thoát")
        return 0
    # đánh dấu dedup bằng comment ẩn trong alert
    alert = build_alert(fresh, a.mode, n)
    marks = "".join(f"<!-- {dedup_key(a.mode, r['t'], date_s)} -->" for r in fresh)
    anchor = "<h2>🦈 CÁ MẬP GOM HÀNG"  # có <h2> để không nhầm comment CSS chứa cùng chữ
    i = h.find(anchor)
    if i == -1:
        print("không thấy anchor CÁ MẬP GOM HÀNG", file=sys.stderr)
        return 2
    j = h.find("</h2>", i) + len("</h2>")
    h = h[:j] + "\n" + marks + alert + h[j:]

    # 🦈 Tom lệnh 16/10: gap premarket ≥4% là tín hiệu cá mập — đưa thẳng vào urgentBox ngay,
    # không chờ cron 5 phút. Mỗi mã 1 alert, có data-ticker để daily reset dọn được.
    big = [r for r in fresh if abs(r["gap"]) >= 4.0][:6]
    if big:
        sharks = []
        for r in big:
            d = "lên" if r["gap"] > 0 else "xuống"
            heat = "heat-green" if r["gap"] > 0 else "heat-red"
            sharks.append(
                f'<div class="alert urgent" data-ticker="{r["t"]}" data-sig-px="{r["px"]:.2f}">'
                f'<span class="heat {heat}">{"🟢 CÒN KỊP" if r["gap"] > 0 else "🔴 COI CHỪNG"}</span><br>'
                f'<b>🦈🦈 GAP PREMARKET {fmt_pct(r["gap"])} — {r["t"]} gap {d} mạnh (~${r["px"]:,.2f})</b><br>'
                f'<span class="t">🦈 Cá mập • {date_s} • gap premarket {a.mode} • chỉ tham khảo</span></div>')
        ub = h.find('<div id="urgentBox">')
        if ub != -1:
            # chèn sau sharkday (nếu có) hoặc ngay sau mở div
            ins = h.find('<div class="alert urgent"', ub)
            if ins == -1:
                ins = h.find('>', ub) + 1
            h = h[:ins] + "\n".join(sharks) + "\n" + h[ins:]
            print(f"  + {len(sharks)} shark alert vào urgentBox", flush=True)
        else:
            # 19/10: đã từng mất urgentBox mà skip im lặng → báo to để worker xử lý
            print("WARN: không thấy <div id=\"urgentBox\"> — shark alert KHÔNG chèn được", file=sys.stderr)

    # validate nhanh
    if h.count("<div") < h.count("</div>") - 5:
        print("VALIDATE FAIL: div mất cân", file=sys.stderr)
        return 2
    open(a.html, "w", encoding="utf-8").write(h)
    repo_html = os.path.join(a.repo_dir, "index.html")
    if os.path.abspath(a.html) != os.path.abspath(repo_html):
        open(repo_html, "w", encoding="utf-8").write(h)
    # git push (không add .github)
    cwd = os.getcwd()
    os.chdir(a.repo_dir)
    msg = f"gap scan {a.mode} {n.strftime('%d/%m %H:%M')} PT"
    res = "failed"
    for attempt in range(3):
        os.system("git add index.html zone_state.json market_state.json >/dev/null 2>&1")
        rc = os.system(f'git -c user.email="tom@local" -c user.name="Tom" commit -qm "{msg}" >/dev/null 2>&1')
        rc = os.system("git pull --rebase origin main >/dev/null 2>&1")
        rc = os.system("git push origin main >/dev/null 2>&1")
        if rc == 0:
            res = "pushed"
            break
    os.chdir(cwd)
    gaps = ", ".join(f"{r['t']} {fmt_pct(r['gap'])}" for r in fresh[:8])
    print(f"gap {a.mode}: {gaps} | git: {res}", flush=True)
    # 📓 ghi signal vào journal để học (Tom lệnh 15/10: script phải học)
    try:
        jp = os.path.join(a.repo_dir, "signal_journal.jsonl")
        n = now_pt()
        with open(jp, "a", encoding="utf-8") as f:
            for r in fresh:
                rec = {"ts": n.isoformat(), "date": n.strftime("%Y-%m-%d"),
                       "type": "gap", "ticker": r["t"],
                       "price": round(float(r.get("open", r["px"])), 2),
                       "extra": f"gap {r['gap']:+.1f}%"}
                f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"  ! gap journal: {e}", file=sys.stderr)
    return 0 if res in ("pushed", "nothing-to-commit") else 3


if __name__ == "__main__":
    sys.exit(main())
