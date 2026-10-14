#!/usr/bin/env python3
"""quick_refresh.py — làm tươi GIÁ + phát hiện VÀO VÙNG cho bảng Live trong ~30 giây.

Dùng Yahoo Finance API (nhanh, không cần key) thay vì quét tay từng mã.
Chạy: every 5-15 phút trong giờ giao dịch (GitHub Action + cron agent).

Usage:
  python3 quick_refresh.py --html PATH --repo-dir PATH [--mode price|full]

  --mode price: chỉ sửa giá/% + badge giờ (cho GitHub Action 5 phút)
  --mode full:  thêm phát hiện vào vùng/gãy cắt/reclaim (cho cron 15 phút)
"""
import argparse, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen

PT = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ticker -> Yahoo symbol suffix (mặc định .us không cần cho Yahoo, dùng symbol trần)
TICKER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refresh-tickers.txt")

ZONES = {
    "AMZN": {"entry": (246, 250), "book": "Sổ 2"},
    "MRVL": {"entry": (260, 270), "reclaim": (275, 285), "cut": 240, "book": "Sổ 2",
             "pos": "2 shares @ ~$266–267"},
    "RKLB": {"entry": (64, 66), "reclaim": (70, 72), "cut": 61.50, "book": "Sổ 3"},
    "NFLX": {"entry": (65, 70), "cut": 63, "book": "Sổ 3"},
    "HIMS": {"entry": (27, 28.50), "cut": 26, "book": "Sổ 3"},
    "CRDO": {"entry": (185, 195), "cut": 178, "targets": (220, 240), "book": "Sổ 2"},
    "JPM":  {"entry": (323, 333), "cut": 315.70, "book": "Sổ 2"},
    "SNDK": {"entry": (1700, 1740), "cut": 1650, "book": "Sổ 2"},
    "NOC":  {"entry": (480, 505), "book": "Sổ 1"},
    "KLAC": {"entry": (170, 185), "book": "Sổ 1"},
    "HWM":  {"entry": (200, 220), "book": "Sổ 1"},
}

# Còi báo động market — Tom lệnh 14/10: tự phát hiện market pump/dump, không chờ ai nhắc
MARKET_ETFS = ["SPY", "QQQ", "DIA"]
MKT_DUMP_AT = -1.5   # S&P 500 <= -1.5% trong ngày -> banner đỏ
MKT_PUMP_AT = 1.5    # S&P 500 >= +1.5% trong ngày -> banner xanh

BADGES = {
    "IN":     '<span class="badge b-in">🟢 TRONG VÙNG</span>',
    "BELOW":  '<span class="badge b-near">🟡 DƯỚI VÙNG</span>',
    "BROKEN": '<span class="badge b-cut">🔴 GÃY VÙNG</span>',
    "ABOVE":  '<span class="badge b-out">⚪ TRÊN VÙNG</span>',
}
STATE_VN = {"IN": "vào vùng", "BELOW": "dưới vùng", "BROKEN": "gãy cắt", "ABOVE": "trên vùng"}


def now_pt():
    return datetime.now(PT)


def in_window():
    n = now_pt()
    if n.weekday() >= 5:  # T7, CN
        return False
    hm = n.hour * 60 + n.minute
    return 6 * 60 + 30 <= hm <= 13 * 60


def fetch_one(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
    try:
        req = Request(url, headers=UA)
        with urlopen(req, timeout=10) as r:
            d = json.load(r)["chart"]["result"][0]["meta"]
        price = float(d["regularMarketPrice"])
        prev = float(d.get("chartPreviousClose") or d.get("previousClose") or price)
        pct = (price - prev) / prev * 100 if prev else 0.0
        return ticker, (round(price, 2), round(pct, 2))
    except Exception as e:
        print(f"  ! {ticker}: {e}", file=sys.stderr)
        return ticker, None


def fetch_prices(tickers):
    out = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t, v in ex.map(fetch_one, tickers):
            if v:
                out[t] = v
    return out


def fmt_price(p):
    return f"${p:,.2f}"


def fmt_pct(p):
    s = f"{abs(p):.1f}%"
    return ("−" if p < 0 else "+" if p > 0 else "") + s


def update_hot_entries(h, prices):
    """<b>TICKER ~$X (−Y%)</b> -> số mới. Update TẤT CẢ occurrence."""
    n = 0
    for t, (px, pc) in prices.items():
        pat = re.compile(r"(<b>" + re.escape(t) + r"\s+~?\$)[\d,]+(?:\.\d+)?(\s*\()([^)]*%)(\))",
                         re.S)
        def rep(m):
            return f"{m.group(1)}{px:,.2f}{m.group(2)}{fmt_pct(pc)}{m.group(4)}"
        h, c = pat.subn(rep, h)
        n += c
    return h, n


def discover_tickers(h):
    """Tự quét TẤT CẢ mã trong các bảng của board — board thêm mã nào, script theo mã đó.
    Fix 14/10: trước đây chỉ 15 mã cố định nên các mã hunt (PCVX, RCL...) nằm lì."""
    found = re.findall(r"<tr><td><b>([A-Z]{2,5})</b></td>", h)
    return sorted(set(found) | set(ZONES.keys()))


def update_tables(h, prices):
    """<tr><td><b>TICKER</b></td><td>$X <span class=down/up>Y%</span> — sửa số + class."""
    n = 0
    for t, (px, pc) in prices.items():
        pat = re.compile(
            r"(<tr><td><b>" + re.escape(t) + r"</b></td>\s*<td>~?\$?)[\d,]+(?:\.\d+)?"
            r"(\s*<span class=\")(down|up)(\">)[^<]*(</span>)", re.S)
        cls = "down" if pc < 0 else "up"
        def rep(m):
            return f"{m.group(1)}{px:,.2f}{m.group(2)}{cls}{m.group(4)}{fmt_pct(pc)}{m.group(5)}"
        h, c = pat.subn(rep, h)
        n += c
        # ô giá không có % (vd HOOD $110.60) — chỉ thay số
        pat2 = re.compile(
            r"(<tr><td><b>" + re.escape(t) + r"</b></td>\s*<td>~?\$?)([\d,]+(?:\.\d+)?)(</td>)")
        h, c2 = pat2.subn(lambda m: f"{m.group(1)}{px:,.2f}{m.group(3)}", h)
        n += c2
    return h, n


def zone_state(t, px):
    z = ZONES[t]
    lo, hi = z["entry"]
    if z.get("cut") and px < z["cut"]:
        return "BROKEN"
    if lo <= px <= hi:
        return "IN"
    if px < lo:
        return "BELOW"
    return "ABOVE"


def update_zone_badges(h, prices, state_path):
    """So state mới vs state cũ -> update badge bảng + signal entry nếu đổi."""
    try:
        old = json.load(open(state_path)) if os.path.exists(state_path) else {}
    except Exception:
        old = {}
    new_state = dict(old)
    events = []
    for t in ZONES:
        if t not in prices:
            continue
        px, pc = prices[t]
        st = zone_state(t, px)
        new_state[t] = {"state": st, "price": px, "time": now_pt().isoformat()}
        prev = (old.get(t) or {}).get("state")
        if prev and prev != st:
            events.append((t, prev, st, px, pc))
        # update badge trong table row của ticker này
        row_pat = re.compile(
            r"(<tr><td><b>" + re.escape(t) + r"</b></td>.*?<span class=\"badge )[^\"<>]*(\">)[^<]*(</span>)",
            re.S)
        h = row_pat.sub(lambda m: f"{m.group(1)}{badge_class(st)}{m.group(2)}{badge_text(st)}{m.group(3)}", h, count=1)
    json.dump(new_state, open(state_path, "w"), indent=1)
    return h, events


def badge_class(st):
    return {"IN": "b-in", "BELOW": "b-near", "BROKEN": "b-cut", "ABOVE": "b-out"}[st]


def badge_text(st):
    return {"IN": "🟢 TRONG VÙNG", "BELOW": "🟡 DƯỚI VÙNG",
            "BROKEN": "🔴 GÃY VÙNG", "ABOVE": "⚪ TRÊN VÙNG"}[st]


def add_signals(h, events):
    if not events:
        return h
    ts = now_pt().strftime("%H:%M")
    block = ""
    for t, prev, st, px, pc in events:
        z = ZONES[t]
        extra = ""
        if t == "MRVL" and z.get("pos"):
            pnl = (px - 266.5) * 2
            extra = f" Vị thế Tom ({z['pos']}): {pnl:+.0f}$ tổng."
        desc = {"IN": "mới vào vùng", "BELOW": "rớt khỏi vùng",
                "BROKEN": "THỦNG CẮT", "ABOVE": "vượt lên trên vùng"}[st]
        block += (f'  <div class="alert"><b>🚨 {t} — {desc} {z["book"]}</b> — '
                  f'{fmt_price(px)} ({fmt_pct(pc)}) lúc {ts} PT.{extra}<br>'
                  f'<span class="t">Tín hiệu vùng tự động • {now_pt().strftime("%d/%m")} • giá intraday</span></div>\n')
    anchor = "🚨 TÍN HIỆU VÙNG — mới nhất lên đầu</h2>"
    i = h.find(anchor)
    if i != -1:
        j = h.find("</h2>", i) + len("</h2>")
        h = h[:j] + "\n" + block + h[j:]
    return h


def bump_badge(h, tag):
    n = now_pt()
    stamp = n.strftime("%d/%m/%Y ~%H:%M PT")
    h2, c = re.subn(r"Cập nhật lần cuối:[^<]*", f"Cập nhật lần cuối: {stamp} ({tag})", h, count=1)
    return h2 if c else h


# NHỊP THỊ TRƯỜNG — mục focus #1 của Tom (14/10): phải xếp hạng lại MỖI lượt,
# không để nằm lì như sáng 14/10 (kẹt ở 13/10 cả buổi)
NHIP_EXTRA = ["SKHY", "ASTS", "SMMT", "AMAT", "WDC"]


def refresh_nhip(h, all_prices):
    """Xếp hạng lại ⚡ NHỊP THỊ TRƯỜNG (nóng/lạnh nhất) từ % cùng ngày."""
    ranked = sorted(((t, pc) for t, (px, pc) in all_prices.items() if isinstance(pc, (int, float))),
                    key=lambda x: x[1], reverse=True)
    if len(ranked) < 8:
        return h, "skip (thiếu mã)"
    hot, cold = ranked[:4], ranked[-4:][::-1]
    hot_html = "<br>".join(f"{t} {fmt_pct(pc)}" for t, pc in hot)
    cold_html = "<br>".join(f"{t} {fmt_pct(pc)}" for t, pc in cold)
    n_up = sum(1 for _, pc in ranked if pc > 0)
    n_dn = sum(1 for _, pc in ranked if pc < 0)
    if n_dn >= n_up + 5:
        mood = "Phe bán áp đảo — ưu tiên phòng thủ, chỉ canh cắt cứng."
    elif n_up >= n_dn + 5:
        mood = "Phe mua chiếm ưu thế — canh vùng vào."
    else:
        mood = "Hai phe giằng co."
    note = (f"Tự xếp hạng mỗi 5 phút từ % biến động cùng ngày (Yahoo). "
            f"Hôm nay: {n_up} xanh / {n_dn} đỏ trên {len(ranked)} mã quét. {mood}")
    date_s = now_pt().strftime("%d/%m")
    h, c0 = re.subn(r"⚡ NHỊP THỊ TRƯỜNG — ai nóng ai lạnh \(\d{2}/\d{2}\)",
                    f"⚡ NHỊP THỊ TRƯỜNG — ai nóng ai lạnh ({date_s})", h, count=1)
    h, c1 = re.subn(r'(🔥 NÓNG NHẤT</div><div class="val up" style="font-size:14px;line-height:1.9">).*?(</div>)',
                    r"\g<1>" + hot_html + r"\g<2>", h, count=1, flags=re.S)
    h, c2 = re.subn(r'(❄️ LẠNH NHẤT</div><div class="val down" style="font-size:14px;line-height:1.9">).*?(</div>)',
                    r"\g<1>" + cold_html + r"\g<2>", h, count=1, flags=re.S)
    h, c3 = re.subn(r'(❄️ LẠNH NHẤT</div><div class="val down" style="font-size:14px;line-height:1.9">.*?</div></div>\s*</div>\s*<div class="note">).*?(</div>)',
                    r"\g<1>" + note + r"\g<2>", h, count=1, flags=re.S)
    ok = (c0 + c1 + c2 + c3) == 4
    return h, (f"nóng: {', '.join(t for t, _ in hot)} / lạnh: {', '.join(t for t, _ in cold)}"
               if ok else f"thiếu pattern ({c0}{c1}{c2}{c3})")


def market_klaxon(h, repo_dir):
    """Tự phát hiện market pump/dump qua SPY/QQQ/DIA — dựng banner đầu bảng.
    Không chờ cron phân tích, không chờ Tom nhắc."""
    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            mkt = dict(ex.map(fetch_one, MARKET_ETFS))
    except Exception as e:
        print(f"  ! market fetch: {e}", file=sys.stderr)
        return h, None
    mkt = {k: v for k, v in mkt.items() if v}
    if "SPY" not in mkt:
        return h, None
    spy_px, spy_pc = mkt["SPY"]
    qqq_pc = mkt.get("QQQ", (0, 0))[1]
    dia_pc = mkt.get("DIA", (0, 0))[1]

    state_path = os.path.join(repo_dir, "market_state.json")
    try:
        prev = json.load(open(state_path)) if os.path.exists(state_path) else {}
    except Exception:
        prev = {}
    prev_regime = prev.get("regime")

    if spy_pc <= MKT_DUMP_AT:
        regime = "DUMP"
    elif spy_pc >= MKT_PUMP_AT:
        regime = "PUMP"
    else:
        regime = None

    # xóa banner cũ (nếu có)
    h = re.sub(r'  <div class="alert mkt" id="mkt-klaxon">.*?</div>\n', "", h, flags=re.S)

    banner = None
    if regime:
        last_pc = prev.get("pc")
        # chỉ dựng/cập nhật khi mới xuất hiện hoặc biến động thêm ±0.5%
        if prev_regime != regime or (isinstance(last_pc, (int, float)) and abs(spy_pc - last_pc) >= 0.5):
            icon = "🔴" if regime == "DUMP" else "🟢"
            word = "DUMP" if regime == "DUMP" else "PUMP"
            cls = "hot" if regime == "DUMP" else "good"
            banner = (f'  <div class="alert mkt {cls}" id="mkt-klaxon"><b>{icon} MARKET {word} '
                      f"— S&P 500 ({fmt_pct(spy_pc)}), Nasdaq ({fmt_pct(qqq_pc)}), Dow ({fmt_pct(dia_pc)})</b> — "
                      f"tự phát hiện lúc {now_pt().strftime('%H:%M')} PT. Vùng mua có thể thành dao rơi — "
                      f"chỉ canh cắt cứng.<br>"
                      f'<span class="t">Còi báo động tự động • giá intraday</span></div>\n')
            # chèn ngay sau dòng badge cập nhật
            h = re.sub(r'(<span class="updated">.*?</span>)', r"\1\n" + banner, h, count=1)
    json.dump({"regime": regime, "pc": round(spy_pc, 2),
               "time": now_pt().isoformat()}, open(state_path, "w"))
    return h, regime


def validate(h):
    for t in ["div", "table", "tr", "script"]:
        if len(re.findall(f"<{t}[ >]", h)) != h.count(f"</{t}>"):
            return False, f"thẻ {t} lệch"
    if "PIN-GATE-START" not in h or "BOARD_PIN" not in h:
        return False, "PIN gate mất"
    return True, "ok"


def git_push(repo_dir, msg):
    def run(*a):
        return subprocess.run(a, cwd=repo_dir, capture_output=True, text=True, timeout=120)
    run("git", "pull", "--rebase", "origin", "main")
    # add có chọn lọc — KHÔNG add .github/ (token thiếu workflow scope, push sẽ bị từ chối)
    run("git", "add", "index.html", "zone_state.json", "market_state.json",
        "quick_refresh.py", "refresh-tickers.txt")
    st = run("git", "status", "--porcelain").stdout.strip()
    if not st:
        return "nothing-to-commit"
    run("git", "-c", "user.email=tom@local", "-c", "user.name=Tom", "commit", "-qm", msg)
    for i in range(3):
        r = run("git", "push", "origin", "main")
        if r.returncode == 0:
            return "pushed"
        time.sleep(30)
    return "push-failed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True)
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--mode", default="price", choices=["price", "full"])
    a = ap.parse_args()

    if not in_window():
        print("ngoài giờ giao dịch — thoát im lặng")
        return 0

    tickers = discover_tickers(open(a.html, encoding="utf-8").read())
    print(f"quét thấy {len(tickers)} mã trên bảng", flush=True)
    print(f"lấy giá {len(tickers)} mã...", flush=True)
    prices = fetch_prices(tickers)
    print(f"ok {len(prices)}/{len(tickers)}", flush=True)
    if not prices:
        print("không lấy được giá nào — thoát", file=sys.stderr)
        return 1

    h = open(a.html, encoding="utf-8").read()
    # header ngày của cột giá theo đúng hôm nay (fix 14/10: kẹt "Giá 13/10")
    h = re.sub(r"<th>Giá \d{2}/\d{2}</th>",
               f"<th>Giá {now_pt().strftime('%d/%m')}</th>", h)
    h, n1 = update_hot_entries(h, prices)
    h, n2 = update_tables(h, prices)
    print(f"đã sửa {n1} hot entries, {n2} table cells", flush=True)

    # NHỊP THỊ TRƯỜNG — xếp hạng lại mỗi lượt (mục focus #1)
    try:
        nhip_extra = fetch_prices(NHIP_EXTRA)
    except Exception as e:
        print(f"  ! nhip extra: {e}", file=sys.stderr)
        nhip_extra = {}
    h, nn = refresh_nhip(h, {**prices, **nhip_extra})
    print(f"nhịp thị trường: {nn}", flush=True)

    tag = "giá"
    if a.mode == "full":
        state_path = os.path.join(a.repo_dir, "zone_state.json")
        h, events = update_zone_badges(h, prices, state_path)
        if events:
            h = add_signals(h, events)
            tag = "tín hiệu vùng"
            print(f"tín hiệu vùng mới: {[(t, s) for t, _, s, _, _ in events]}", flush=True)

    # còi báo động market — chạy mọi mode, không chờ ai nhắc
    h, regime = market_klaxon(h, a.repo_dir)
    if regime:
        tag = "còi market"
        print(f"market klaxon: {regime}", flush=True)

    h = bump_badge(h, tag)
    ok, why = validate(h)
    if not ok:
        print(f"VALIDATE FAIL: {why}", file=sys.stderr)
        return 2
    open(a.html, "w", encoding="utf-8").write(h)

    # copy sang repo nếu khác file
    repo_html = os.path.join(a.repo_dir, "index.html")
    if os.path.abspath(a.html) != os.path.abspath(repo_html):
        open(repo_html, "w", encoding="utf-8").write(h)

    msg = f"refresh {now_pt().strftime('%d/%m %H:%M')} PT ({tag})"
    res = git_push(a.repo_dir, msg)
    print("git:", res, flush=True)
    return 0 if res in ("pushed", "nothing-to-commit") else 3


if __name__ == "__main__":
    sys.exit(main())
