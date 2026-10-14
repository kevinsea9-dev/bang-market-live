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
    run("git", "add", "-A")
    st = run("git", "status", "--porcelain").stdout.strip()
    if not st:
        return "nothing-to-commit"
    run("git", "-c", "user.email=tom@local", "-c", "user.name=Tom", "commit", "-qm", msg)
    for i in range(3):
        r = run("git", "push", "origin", "main")
        if r.returncode == 0:
            return "pushed"
        time.sleep(120)
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

    tickers = [l.strip() for l in open(TICKER_FILE) if l.strip() and not l.startswith("#")]
    print(f"lấy giá {len(tickers)} mã...", flush=True)
    prices = fetch_prices(tickers)
    print(f"ok {len(prices)}/{len(tickers)}", flush=True)
    if not prices:
        print("không lấy được giá nào — thoát", file=sys.stderr)
        return 1

    h = open(a.html, encoding="utf-8").read()
    h, n1 = update_hot_entries(h, prices)
    h, n2 = update_tables(h, prices)
    print(f"đã sửa {n1} hot entries, {n2} table cells", flush=True)

    tag = "giá"
    if a.mode == "full":
        state_path = os.path.join(a.repo_dir, "zone_state.json")
        h, events = update_zone_badges(h, prices, state_path)
        if events:
            h = add_signals(h, events)
            tag = "tín hiệu vùng"
            print(f"tín hiệu vùng mới: {[(t, s) for t, _, s, _, _ in events]}", flush=True)

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
