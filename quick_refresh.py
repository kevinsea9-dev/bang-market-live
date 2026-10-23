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

# zones.json là NGUỒN CHÂN LÝ DUY NHẤT (29 vùng đã duyệt, Tom cấp quyền tự động 14/10).
# Dict cứng bên dưới chỉ là fallback khi file mất.
ZONES_FALLBACK = {
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

ZONES = {}


def load_zones(repo_dir):
    """Đọc zones.json; rớt về fallback nếu file mất."""
    global ZONES
    zp = os.path.join(repo_dir, "zones.json")
    try:
        raw = json.load(open(zp, encoding="utf-8"))
        ZONES = {t: v for t, v in raw.items() if not t.startswith("_")}
        print(f"zones: {len(ZONES)} vùng từ zones.json", flush=True)
    except Exception as e:
        ZONES = dict(ZONES_FALLBACK)
        print(f"zones: dùng fallback ({e})", file=sys.stderr, flush=True)
    return ZONES

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
    # 04:30–13:00 PT (mở rộng premarket 22/10 — trước đây 06:30 khiến bảng,
    # đặc biệt dải Bull & Bear, đứng hình suốt premarket; ngoài giờ chính
    # Bull & Bear đọc futures nên số vẫn tươi và đúng nguồn)
    return 4 * 60 + 30 <= hm <= 13 * 60


def fetch_one(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
    try:
        req = Request(url, headers=UA)
        with urlopen(req, timeout=10) as r:
            d = json.load(r)["chart"]["result"][0]["meta"]
        price = float(d["regularMarketPrice"])
        prev = float(d.get("chartPreviousClose") or d.get("previousClose") or price)
        pct = (price - prev) / prev * 100 if prev else 0.0
        low = float(d.get("regularMarketDayLow") or 0)
        high52 = float(d.get("fiftyTwoWeekHigh") or 0)
        return ticker, (round(price, 2), round(pct, 2), round(low, 2), round(high52, 2))
    except Exception as e:
        print(f"  ! {ticker}: {e}", file=sys.stderr)
        return ticker, None


def fetch_prices(tickers):
    """Trả về (prices, daylows, highs52): prices[t]=(giá, %) giữ nguyên để code cũ không vỡ;
    daylows[t]=đáy ngày phục vụ dò bounce; highs52[t]=đỉnh 52 tuần phục vụ săn hàng giảm giá."""
    out, lows, highs = {}, {}, {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t, v in ex.map(fetch_one, tickers):
            if v:
                out[t] = (v[0], v[1])
                if v[2]:
                    lows[t] = v[2]
                if len(v) > 3 and v[3]:
                    highs[t] = v[3]
    return out, lows, highs


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


def dedup_section_alerts(h, section_marker):
    """Trong một mục alert (tín hiệu vùng / mã nóng): mỗi ticker chỉ giữ 1 —
    cái mới nhất (đầu tiên, vì tín hiệu mới chèn lên đầu). Tom lệnh 14/10:
    'mỗi mã một vị', trùng thì tự biến mất."""
    i = h.find(section_marker)
    if i == -1:
        return h, []
    j = h.find("</h2>", i) + len("</h2>")
    k = h.find('<div class="sec">', j)
    if k == -1:
        k = len(h)
    sec = h[j:k]
    seen = set()
    removed = []

    def keep_first(m):
        t = m.group(1)
        if t in seen:
            removed.append(f"{t}(alert trùng)")
            return ""
        seen.add(t)
        return m.group(0)

    sec2 = re.sub(r'<div class="alert[^"]*"><b>(?:🚨 )?([A-Z]{2,5})\b.*?</div>',
                  keep_first, sec, flags=re.S)
    return h[:j] + sec2 + h[k:], removed


def dedup_urgentbox(h):
    """Tom lệnh 15/10 (~11:25): bảng KHẨN bị trùng lặp — mỗi mã một alert mới nhất.
    Urgent alert không bọc <b> quanh ticker (🔥 TTD BOUNCE...) nên regex cũ
    của dedup_section_alerts không bắt được. Hàm này tách ticker = token
    HOA đầu tiên trong alert, giữ cái mới nhất (alert mới chèn lên đầu)."""
    i = h.find('id="urgentBox"')
    if i == -1:
        return h, []
    j = h.find(">", i) + 1
    k = h.find('<div class="sec"', j)
    if k == -1:
        k = len(h)
    box = h[j:k]
    seen_tk = set()
    seen_no_tk = set()
    removed = []

    def keep_first(m):
        full, inner = m.group(0), m.group(1)
        txt = re.sub(r'<[^>]+>', ' ', inner).strip()
        mt = re.search(r'\b([A-Z]{2,5})\b', txt)
        if mt:
            t = mt.group(1)
            if t in seen_tk:
                removed.append(f"{t}(khẩn trùng)")
                return ""
            seen_tk.add(t)
            return full
        norm = re.sub(r'\s+', ' ', txt).strip()[:60].lower()
        if norm in seen_no_tk:
            removed.append("(khẩn trùng nội dung)")
            return ""
        seen_no_tk.add(norm)
        return full

    box2 = re.sub(r'<div class="alert urgent">(.*?)</div>', keep_first, box, flags=re.S)
    # Tom 16/10: dedup mạnh theo data-ticker — 1 mã chỉ 1 alert mới nhất
    # (capitulation/moneyflow/reversal tạo alert riêng, regex text không bắt được)
    seen_dt = set()
    def keep_dt(m):
        full = m.group(0)
        mt = re.search(r'data-ticker="([A-Z]{2,6})"', full)
        if mt:
            t = mt.group(1)
            if t in seen_dt:
                removed.append(f"{t}(trùng data-ticker)")
                return ""
            seen_dt.add(t)
        return full
    box3 = re.sub(r'<div class="alert urgent" data-ticker="[^"]*".*?</div>', keep_dt, box2, flags=re.S)
    return h[:j] + box3 + h[k:], removed


def zone_tp_line(t):
    """Dòng vùng/Chốt/Cắt gọn cho alert khẩn + HOT feed.
    Tom góp ý 15/10 (~13:00): alert nào cũng phải thấy ngay chốt ở đâu.
    -> '🎯 Chốt $613→$658 · Cắt <$520' (Sổ 2)"""
    z = ZONES.get(t)
    if not z or not isinstance(z, dict):
        return ""
    parts = []
    tg = z.get("targets") or []
    if tg:
        parts.append("Chốt $" + "→$".join(f"{x:g}" for x in tg))
    cut = z.get("cut")
    if cut:
        parts.append(f"Cắt <${cut:g}")
    if not parts:
        return ""
    book = z.get("book", "")
    suffix = f" ({book})" if book else ""
    return f"🎯 {' · '.join(parts)}{suffix}"


def attach_zone_tp(h):
    """Tom lệnh 15/10 (~13:00): gắn vùng giá + TP vào mọi alert khẩn.
    Chạy SAU dedup để không phá logic trùng. HOT feed (JS buildHotFeed)
    clone alert nên tự có dòng này theo. Alert đã có dòng thì bỏ qua."""
    i = h.find('id="urgentBox"')
    if i == -1:
        return h
    j = h.find(">", i) + 1
    k = h.find('<div class="sec"', j)
    if k == -1:
        k = len(h)
    box = h[j:k]

    def add_tp(m):
        full, inner = m.group(0), m.group(1)
        if "🎯" in inner:
            return full
        txt = re.sub(r'<[^>]+>', ' ', inner).strip()
        mt = re.search(r'\b([A-Z]{2,5})\b', txt)
        if not mt:
            return full
        line = zone_tp_line(mt.group(1))
        if not line:
            return full
        return full.replace("</div>",
                            f'<br><span class="zt">{line}</span></div>', 1)

    box2 = re.sub(r'<div class="alert urgent"[^>]*>(.*?)</div>', add_tp, box, flags=re.S)
    return h[:j] + box2 + h[k:]


# ============================================================================
# 🦈 CÁ MẬP x3 — Tom duyệt tối 15/10 ("Ok em"):
#   1. 🔔 ĐÃ VỀ ĐIỂM CHỜ — mã 🟡 CHỜ PULLBACK quay về ±1% giá signal -> badge
#   2. 🔢 Sóng thứ mấy — đếm alert cá mập/mã/ngày, sóng ≥3 cảnh báo đu đỉnh
#   3. 👑 Cá mập của ngày — top 3 theo tier × tin cậy × độ tươi
# ============================================================================
SHARK_CSS = (
    ".backwait{display:inline-block;font-size:12.5px;font-weight:800;border-radius:8px;"
    "padding:3px 10px;margin:2px 4px 2px 0;background:rgba(34,211,238,.18);color:#67e8f9;"
    "border:1px solid #22d3ee;animation:heatBlink 1.2s infinite}"
    ".wave{display:inline-block;font-size:12px;font-weight:700;border-radius:8px;"
    "padding:2px 9px;margin:2px 4px 2px 0;background:rgba(167,139,250,.15);color:#c4b5fd;"
    "border:1px solid #8b5cf6}"
    ".wave.warn{background:rgba(220,38,38,.18);color:#fca5a5;border-color:#dc2626}"
    ".sharkday{border:2px solid #ffc94d;border-radius:12px;padding:10px 12px;margin:8px 0;"
    "background:linear-gradient(135deg,rgba(255,201,77,.14),rgba(20,16,7,.6))}"
    ".sharkday h3{margin:0 0 6px;font-size:15px;color:#ffd76a}"
    ".sharkday .sd-row{font-size:13.5px;padding:3px 0;color:#e8eefc}"
    ".sharkday .sd-why{color:#9fb3d9;font-size:12.5px}"
)


def ensure_shark_css(h):
    """Chèn CSS 3 tính năng cá mập nếu chưa có (idempotent).
    Check '.backwait{' (khai báo CSS), không phải 'backwait' (trùng class trong badge HTML)."""
    if ".backwait{" in h or "</style>" not in h:
        return h
    return h.replace("</style>", SHARK_CSS + "\n</style>", 1)


GATE_CSS = (
    ".gate-open{border:2px solid #ffc94d !important;border-radius:12px;"
    "animation:gatePulse 1.6s ease-in-out infinite}"
    "@keyframes gatePulse{0%,100%{box-shadow:0 0 6px rgba(255,201,77,.35)}"
    "50%{box-shadow:0 0 22px rgba(255,201,77,.9)}}"
)

DEAL_CSS = (
    ".alert.special-deal{border:2px solid #7ee7f5 !important;border-radius:10px;"
    "background:linear-gradient(135deg,rgba(126,231,245,.12),rgba(255,201,77,.10));"
    "animation:dealPulse 1.4s ease-in-out infinite}"
    "@keyframes dealPulse{0%,100%{box-shadow:0 0 5px rgba(126,231,245,.3)}"
    "50%{box-shadow:0 0 20px rgba(126,231,245,.85)}}"
)

def ensure_gate_css(h):
    """Tom 19/10: CSS chớp nháy cho đồng hồ cổng săn (idempotent)."""
    if "</style>" not in h:
        return h
    if ".gate-open{" not in h:
        h = h.replace("</style>", GATE_CSS + "\n</style>", 1)
    # 💎 CSS tín hiệu đặc biệt — thêm riêng để không chớp trùng với bản cũ
    if ".special-deal{" not in h:
        h = h.replace("</style>", DEAL_CSS + "\n</style>", 1)
    return h


def _journal_latest(repo_dir, ticker, today):
    """(sig_px, sig_ts) mới nhất của ticker hôm nay từ journal — fallback cho alert cũ."""
    jp = os.path.join(repo_dir, "signal_journal.jsonl")
    px, ts = None, None
    try:
        with open(jp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("ticker") != ticker or str(d.get("date", "")) != today:
                    continue
                try:
                    if d.get("price"):
                        px = float(d["price"])
                except Exception:
                    pass
                if d.get("ts"):
                    ts = d["ts"]
    except Exception:
        pass
    return px, ts


def mark_back_to_wait(h, prices, repo_dir):
    """🔔 ĐÃ VỀ ĐIỂM CHỜ — Tom duyệt 15/10 tối.
    Alert 🟡 CHỜ PULLBACK nào có giá hiện tại quay về trong ±1% giá lúc
    alert -> gắn badge '🔔 ĐÃ VỀ ĐIỂM CHỜ'. Mỗi mã 1 lần/ngày (state file)."""
    today = now_pt().strftime("%Y-%m-%d")
    sp = os.path.join(repo_dir, "back_to_wait_state.json")
    state = {}
    try:
        if os.path.exists(sp):
            state = json.load(open(sp, encoding="utf-8"))
    except Exception:
        state = {}
    if state.get("date") != today:
        state = {"date": today, "ticked": []}
    ticked = set(state.get("ticked", []))

    i = h.find('id="urgentBox"')
    if i == -1:
        return h, "về điểm chờ: thiếu #urgentBox"
    j = h.find(">", i) + 1
    k = h.find('<div class="sec"', j)
    if k == -1:
        k = len(h)
    box = h[j:k]
    n = [0]

    def check(m):
        full, inner = m.group(0), m.group(1)
        if "heat-yellow" not in inner or "ĐÃ VỀ ĐIỂM CHỜ" in inner:
            return full
        mt = re.search(r'data-ticker="([A-Z]{1,6})"', full)
        if not mt:
            return full
        t = mt.group(1)
        if t in ticked:
            return full
        ms = re.search(r'data-sig-px="([\d.]+)"', full)
        sig_px = float(ms.group(1)) if ms else None
        if not sig_px:
            sig_px, _ = _journal_latest(repo_dir, t, today)
        if not sig_px:
            return full
        px = (prices.get(t) or (0, 0))[0]
        if not px or abs(px - sig_px) / sig_px > 0.01:
            return full
        ticked.add(t)
        n[0] += 1
        badge = '<span class="backwait">🔔 ĐÃ VỀ ĐIỂM CHỜ</span>'
        full2 = re.sub(r'(<span class="heat heat-yellow">.*?</span>)',
                       r"\1" + badge, full, count=1, flags=re.S)
        return full2 if full2 != full else full.replace("</div>", badge + "</div>", 1)

    box2 = re.sub(r'<div class="alert urgent"[^>]*>(.*?)</div>', check, box, flags=re.S)
    if n[0]:
        state["ticked"] = sorted(ticked)
        try:
            json.dump(state, open(sp, "w", encoding="utf-8"))
        except Exception as e:
            print(f"  ! back_to_wait_state: {e}", file=sys.stderr)
    h = ensure_shark_css(h[:j] + box2 + h[k:])
    return h, f"về điểm chờ: {n[0]} mã" if n[0] else "về điểm chờ: chưa mã nào về"


def mark_wave_count(h, repo_dir):
    """🔢 Sóng thứ mấy — Tom duyệt 15/10 tối.
    Đếm số alert cá mập (journal type=urgent) của mỗi mã trong ngày
    -> badge '🌊 Sóng N'; sóng ≥3 thêm '⚠️ coi chừng đu đỉnh'."""
    today = now_pt().strftime("%Y-%m-%d")
    waves = {}
    jp = os.path.join(repo_dir, "signal_journal.jsonl")
    try:
        with open(jp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if str(d.get("date", "")) != today or d.get("type") != "urgent":
                    continue
                t = d.get("ticker")
                if t:
                    waves[t] = waves.get(t, 0) + 1
    except Exception as e:
        return h, f"sóng: không đọc được journal ({e})"
    if not waves:
        return h, "sóng: journal hôm nay trống"

    i = h.find('id="urgentBox"')
    if i == -1:
        return h, "sóng: thiếu #urgentBox"
    j = h.find(">", i) + 1
    k = h.find('<div class="sec"', j)
    if k == -1:
        k = len(h)
    box = h[j:k]
    n = [0]

    def check(m):
        full, inner = m.group(0), m.group(1)
        mt = re.search(r'data-ticker="([A-Z]{1,6})"', full)
        if not mt:
            return full
        wv = waves.get(mt.group(1), 0)
        if wv < 2:
            new_full = re.sub(r'<span class="wave[^"]*">.*?</span>', "", full, flags=re.S)
            return new_full
        warn = wv >= 3
        badge = (f'<span class="wave{" warn" if warn else ""}">🌊 Sóng {wv}'
                 + (" ⚠️ coi chừng đu đỉnh" if warn else "") + "</span>")
        if re.search(r'<span class="wave[^"]*">', full):
            # đã có badge: chỉ thay nội dung (sóng có thể tăng), không chèn thêm
            new_full = re.sub(r'<span class="wave[^"]*">.*?</span>', badge, full, flags=re.S)
        else:
            new_full = re.sub(r'(<span class="heat[^"]*">.*?</span>)',
                              r"\1" + badge, full, count=1, flags=re.S)
            if new_full == full:
                new_full = full.replace("<b>", badge + "<b>", 1)
        if new_full != full:
            n[0] += 1
        return new_full

    box2 = re.sub(r'<div class="alert urgent"[^>]*>(.*?)</div>', check, box, flags=re.S)
    h = ensure_shark_css(h[:j] + box2 + h[k:])
    return h, f"sóng: đã gắn {n[0]} alert"


def _remove_balanced_div(box, marker):
    """Xóa cả khối <div ...marker...>...</div> cân bằng (kể cả div lồng nhau)."""
    i = box.find(marker)
    if i == -1:
        return box
    j = box.find(">", i) + 1
    depth, k = 1, j
    while depth > 0:
        no, nc = box.find("<div", k), box.find("</div>", k)
        if nc == -1:
            return box
        if no != -1 and no < nc:
            depth += 1
            k = no + 4
        else:
            depth -= 1
            k = nc + 6
    return box[:i] + box[k:].lstrip()


def shark_of_day(h, repo_dir):
    """👑 Cá mập của ngày — Tom duyệt 15/10 tối.
    Top 3 alert trong #urgentBox theo điểm = tier_weight × tin cậy × độ tươi.
    tier: 🦈🦈🦈=3, 🦈🦈=2, 🦈=1 (đếm ở headline). Tin cậy: parse 'Đúng X%'.
    Tươi: từ data-ts (fallback journal), decay theo giờ."""
    today = now_pt().strftime("%Y-%m-%d")
    i = h.find('id="urgentBox"')
    if i == -1:
        return h, "cá mập của ngày: thiếu #urgentBox"
    j = h.find(">", i) + 1
    k = h.find('<div class="sec"', j)
    if k == -1:
        k = len(h)
    box = h[j:k]
    # 21/10: xóa placeholder "chưa có tín hiệu cá mập" rác tích tụ từ daily_reset các
    # ngày trước (không phải alert urgent nên vòng dọn cũ bỏ sót — Tom phàn nàn
    # "cài cron mọi nơi vẫn chưa bay được cá mập" trong khi bảng ghi chưa có tín hiệu).
    box = box.replace('<div class="alert" style="opacity:.75">🦈 Chưa có tín hiệu cá mập hôm nay — radar đang quét, có là báo ngay.</div>', "")
    now = now_pt()
    # 19/10: mã trong _excluded (Tom xóa khỏi bảng) không được lên cá mập của ngày
    try:
        excluded_sd = set(json.load(open(os.path.join(repo_dir, "zones.json"))).get("_excluded", []))
    except Exception:
        excluded_sd = set()
    cands = []
    for m in re.finditer(r'<div class="alert urgent"[^>]*>(.*?)</div>', box, flags=re.S):
        full, inner = m.group(0), m.group(1)
        mt = re.search(r'data-ticker="([A-Z]{1,6})"', full)
        if not mt:
            continue
        t = mt.group(1)
        if t in excluded_sd:
            continue
        # 21/10: alert GẤU (gap xuống, coi chừng, gãy/thủng...) KHÔNG được lên ngôi
        # cá mập của ngày — vụ NFLX gap -5.8% bị xếp 🥇 sáng 21/10.
        _mh0 = re.search(r"<b>(.*?)</b>", inner, flags=re.S)
        _head = re.sub(r"<[^>]+>", "", _mh0.group(1)).lower() if _mh0 else ""
        if any(w in _head for w in ("coi chừng", "🔴", "gãy", "thủng", "dump", "gap xuống", "gap down", "lao dốc")):
            continue
        mh_tier = re.search(r"<b>(🦈+)", inner)
        tier_n = len(mh_tier.group(1)) if mh_tier else 1
        tier_w = 3 if tier_n >= 3 else (2 if tier_n == 2 else 1)
        mtr = re.search(r"Đúng (\d+)%", inner)
        trust = (int(mtr.group(1)) / 100) if mtr else 0.5
        age_h = None
        mts = re.search(r'data-ts="([^"]+)"', full)
        if mts:
            try:
                age_h = max(0, (now - datetime.fromisoformat(mts.group(1))).total_seconds() / 3600)
            except Exception:
                age_h = None
        if age_h is None:
            # 21/10: alert không có data-ts (kiểu cũ, vd gap premarket) -> mặc định 6h.
            # BỎ fallback journal: journal ghi mọi tín hiệu của mã trong ngày nên alert
            # cũ buổi sáng mượn được độ tươi "0p trước" và leo top oan (vụ NFLX 21/10).
            age_h = 6.0
        fresh = 1 / (1 + age_h)
        score = tier_w * trust * fresh
        mh = re.search(r"<b>(.*?)</b>", inner, flags=re.S)
        why = re.sub(r"<[^>]+>", "", mh.group(1)).strip()[:70] if mh else ""
        cands.append((score, t, tier_w, trust, age_h, why))
    box = _remove_balanced_div(box, '<div class="sharkday" id="sharkOfDay"')
    if not cands:
        h = ensure_shark_css(h[:j] + box + h[k:])
        return h, "cá mập của ngày: chưa có alert"
    cands.sort(reverse=True, key=lambda x: x[0])
    medals = ["🥇", "🥈", "🥉"]
    rows = []
    for idx, (score, t, tier_w, trust, age_h, why) in enumerate(cands[:3]):
        age_txt = f"{int(age_h * 60)}p trước" if age_h < 1 else f"{age_h:.1f}h trước"
        rows.append(
            f'<div class="sd-row">{medals[idx]} <b>{t}</b> {"🦈" * tier_w} '
            f'<span class="sd-why">— {why} (tin cậy {trust:.0%}, {age_txt})</span></div>')
    sd = ('<div class="sharkday" id="sharkOfDay">'
          "<h3>👑 CÁ MẬP CỦA NGÀY — top 3 dòng tiền mạnh nhất</h3>"
          + "".join(rows) + "</div>\n")
    box = sd + box
    h = ensure_shark_css(h[:j] + box + h[k:])
    return h, "cá mập của ngày: " + ", ".join(t for _, t, *_ in cands[:3])



def sweep_stale_tickers(h, repo_dir):
    """Tom lệnh 16/10 (~09:35): MÃ CŨ KHÔNG ĐỦ ĐIỀU KIỆN TỰ ĐỘNG XÓA.
    Không đủ điều kiện (luật 3 chân của Tom):
    - ABOVE vùng >15% suốt 3 lượt (chạy xa, không còn rẻ/đẹp), hoặc
    - BROKEN >5 ngày không reclaim (setup chết),
    VÀ không có signal khẩn nào trong 24h qua,
    VÀ KHÔNG trong danh sách bảo vệ (vị thế Tom đang giữ + mã Tom chỉ định).
    3 lượt stale liên tiếp mới xóa (tránh xóa nhầm lúc biến động).
    Xóa = thêm vào zones.json["_excluded"] -> auto_cleanup() xóa dòng."""
    import datetime
    removed = []
    try:
        zones = json.load(open(os.path.join(repo_dir, "zones.json")))
        zs = json.load(open(os.path.join(repo_dir, "zone_state.json")))
    except Exception:
        return h, removed
    # BẢO VỆ: vị thế thật + mã Tom đang theo dõi sát — không bao giờ tự xóa
    # 23/10: thêm SPCX — Tom lệnh 19/10 gỡ SPCX khỏi _excluded; stale-cleanup 22/10
    # từng tự add lại (override lệnh Tom). SPCX có vùng LT duyệt + cron watch riêng.
    protected = {"MRVL", "IREN", "CTVA", "CRDO", "COHR", "GEV", "AMD",
                 "QQQ", "QQQM", "TSLA", "NVDA", "SOXL", "OII", "CMBT", "SPCX"}
    tracker_path = os.path.join(repo_dir, "stale_tracker.json")
    try:
        tracker = json.load(open(tracker_path)) if os.path.exists(tracker_path) else {}
    except Exception:
        tracker = {}
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-7)))
    # signal khẩn 24h qua theo data-ticker
    recent_sig = set()
    for m in re.finditer(r'<div class="alert urgent" data-ticker="([A-Z]{2,6})"[^>]*data-ts="([^"]*)"',
                         h):
        t, ts = m.group(1), m.group(2)
        try:
            dt = datetime.datetime.fromisoformat(ts)
            if (now - dt).total_seconds() < 86400:
                recent_sig.add(t)
        except Exception:
            recent_sig.add(t)
    excluded = zones.get("_excluded", [])
    for t, v in zs.items():
        if t in protected or t in excluded:
            tracker.pop(t, None)
            continue
        state = v.get("state")
        stale = False
        reason = ""
        if state == "ABOVE":
            entry = (zones.get(t) or {}).get("entry", [0, 0])
            px = v.get("price") or 0
            if entry and entry[1] and px:
                over = (px - entry[1]) / entry[1]
                if over > 0.15:
                    stale, reason = True, f"chạy xa vùng +{over*100:.0f}%"
        elif state == "BROKEN":
            try:
                bt = datetime.datetime.fromisoformat(v.get("time", ""))
                if (now - bt).days >= 5:
                    stale, reason = True, f"gãy { (now-bt).days } ngày không reclaim"
            except Exception:
                pass
        if stale and t in recent_sig:
            stale, reason = False, ""  # có signal mới -> còn sống
        if stale:
            tracker[t] = tracker.get(t, 0) + 1
            if tracker[t] >= 3:
                excluded.append(t)
                removed.append(f"{t}({reason})")
                tracker.pop(t, None)
        else:
            tracker.pop(t, None)
    if removed:
        zones["_excluded"] = excluded
        json.dump(zones, open(os.path.join(repo_dir, "zones.json"), "w"), indent=1)
    json.dump(tracker, open(tracker_path, "w"))
    return h, removed

def auto_cleanup(h, prices, tickers, repo_dir):
    """Tom lệnh 14/10: mã nào trùng / chết (không update được) thì TỰ BIẾN MẤT cho đúng Live.
    - Chết: fetch fail 3 lượt liên tiếp -> xóa dòng.
    - Trùng: giữ dòng đầu, xóa các dòng sau.
    - Tom lệnh 15/10: mã trong zones.json["_excluded"] (trung lập) -> xóa dòng,
      không cho cron/worker phục hồi lại."""
    removed = []
    try:
        excluded = json.load(open(os.path.join(repo_dir, "zones.json"))).get("_excluded", [])
    except Exception:
        excluded = []
    for t in excluded:
        h, c = re.subn(r'\s*<tr><td><b>' + re.escape(t) + r'</b></td>.*?</tr>',
                       '', h, flags=re.S)
        if c:
            removed.append(f"{t}(loại theo lệnh Tom)")
    fail_path = os.path.join(repo_dir, "fetch_failures.json")
    try:
        fails = json.load(open(fail_path)) if os.path.exists(fail_path) else {}
    except Exception:
        fails = {}
    for t in tickers:
        if t not in prices:
            fails[t] = fails.get(t, 0) + 1
        else:
            fails.pop(t, None)
    for t, n in list(fails.items()):
        if n >= 3:
            h, c = re.subn(r'\s*<tr><td><b>' + re.escape(t) + r'</b></td>.*?</tr>',
                           '', h, flags=re.S)
            if c:
                removed.append(f"{t}(chết)")
            fails.pop(t, None)
    json.dump(fails, open(fail_path, "w"))
    seen = set()

    def dedup_row(m):
        t = m.group(1)
        if t in seen:
            removed.append(f"{t}(trùng)")
            return ""
        seen.add(t)
        return m.group(0)

    h = re.sub(r'<tr><td><b>([A-Z]{2,5})</b></td>.*?</tr>', dedup_row, h, flags=re.S)
    return h, removed


def prune_old_alerts(h):
    """Tom lệnh 15/10: tin cũ TỰ BIẾN MẤT — chạy mỗi 5 phút, không chờ daily reset.
    - Xóa alert có ngày KHÔNG phải hôm nay (trừ Cú Đêm — xử riêng).
    - Cú Đêm: chỉ giữ 3 entry mới nhất.
    - Xóa alert không có ngày nào (tin rác, trừ mô tả ngắn)."""
    import datetime
    now = now_pt()
    today = now.strftime("%d/%m")
    removed = [0]

    def check_alert(m):
        full, inner = m.group(0), m.group(1)
        # Cú Đêm: để hàm riêng xử, không đụng ở đây
        if 'Cú Đêm' in inner:
            return full
        # Giữ tin hôm nay
        if today in inner:
            return full
        # Xóa tin có ngày cũ
        if re.search(r'\d{1,2}/\d{1,2}', inner):
            removed[0] += 1
            return ''
        # Tin không ngày: giữ nếu là mô tả ngắn, xóa nếu dài (tin rác)
        if len(inner) < 200:
            return full
        removed[0] += 1
        return ''

    h = re.sub(r'<div class="alert[^"]*">(.*?)</div>', check_alert, h, flags=re.S)

    # 📰 TIN TỨC <li> — Tom lệnh 15/10: tin cũ tự động xóa.
    # LUẬT MỚI (Tom lệnh 21/10): mục TIN LỚN chỉ giữ tin TRONG NGÀY — "News hot,
    # trong ngày". Mọi <li> mà stamp không phải hôm nay đều xóa, kể cả tin
    # KHÔNG ghi ngày (luật cũ giữ tin không ngày -> ứ đọng tồn kho vĩnh viễn;
    # và regex '<li>' trơn bỏ sót <li class="newsold"> -> tin mờ thoát dọn).
    try:
        def check_news_li(m):
            full, inner = m.group(0), m.group(1)
            # ngày của tin = stamp <span class="t"> (ngày đăng), fallback ngày đầu trong nội dung
            tm = re.search(r'<span class="t">(\d{1,2})/(\d{1,2})', inner)
            dm = tm if tm else re.search(r'(\d{1,2})/(\d{1,2})', inner)
            if not dm:
                removed[0] += 1
                return ''  # không ngày -> xóa
            d, mo = int(dm.group(1)), int(dm.group(2))
            if f"{d:02d}/{mo:02d}" != today:
                removed[0] += 1
                return ''  # không phải hôm nay -> xóa
            # tin hôm nay: gỡ class mờ newsold sót lại
            return full.replace(' class="newsold"', '')

        # chỉ xử <li> trong mục tin (sec-news), không đụng list khác
        ni = h.find('id="sec-news"')
        if ni != -1:
            nj = h.find('</div>', h.find('</ul>', ni))
            if nj != -1:
                news_sec = h[ni:nj]
                news_sec2 = re.sub(r'<li[^>]*>(.*?)</li>', check_news_li, news_sec, flags=re.S)
                # Quét MẢNH MỒ CÔI (Tom phát hiện 21/10): entry mất thẻ <li> mở
                # do các lần dọn trước -> thân tin nằm trơ ngoài mọi <li>, regex
                # theo cặp thẻ không bắt được. Trong <ul> tin, mọi nội dung nằm
                # ngoài <li>...</li> đều là rác -> dựng lại body chỉ gồm các <li>.
                _ul_m = re.search(r'<ul>(.*)</ul>', news_sec2, flags=re.S)
                if _ul_m:
                    _body = _ul_m.group(1)
                    _residue = re.sub(r'<li[^>]*>.*?</li>', '', _body, flags=re.S)
                    if _residue.strip():
                        _clean = '\n'.join(re.findall(r'<li[^>]*>.*?</li>', _body, flags=re.S))
                        news_sec2 = (news_sec2[:_ul_m.start(1)] + '\n' + _clean +
                                     '\n' + news_sec2[_ul_m.end(1):])
                        removed[0] += 1
                h = h[:ni] + news_sec2 + h[nj:]
    except Exception as e:
        print(f"  ! prune news li: {e}", file=sys.stderr)

    # Tom 16/10: TRẦN CỨNG tin tức — tối đa 30 tin mới nhất, xóa tin cũ nhất khi vượt.
    # (tin mới thêm nhanh hơn tin cũ hết hạn -> ứ đọng 80+ tin)
    try:
        ni2 = h.find('id="sec-news"')
        if ni2 != -1:
            nj2 = h.find('</div>', h.find('</ul>', ni2))
            if nj2 != -1:
                sec = h[ni2:nj2]
                lis = list(re.finditer(r'<li[^>]*>.*?</li>', sec, re.S))
                if len(lis) > 30:
                    for m in reversed(lis[30:]):
                        sec = sec[:m.start()] + sec[m.end():]
                        removed[0] += 1
                    h = h[:ni2] + sec + h[nj2:]
    except Exception as e:
        print(f"  ! news cap: {e}", file=sys.stderr)

    # Chống TRÙNG LẶP nội dung — Tom lệnh 15/10: "đừng để bị trùng lặp"
    seen_text = set()
    def dedup_content(m):
        full, inner = m.group(0), m.group(1)
        # Chuẩn hóa: lấy 80 ký tự đầu của text thuần
        txt = re.sub(r'<[^>]+>', '', inner).strip()[:80].lower()
        txt = re.sub(r'\s+', ' ', txt)
        if txt in seen_text:
            removed[0] += 1
            return ''
        seen_text.add(txt)
        return full
    h = re.sub(r'<div class="alert[^"]*">(.*?)</div>', dedup_content, h, flags=re.S)

    # Cú Đêm: chỉ giữ entry của ĐÊM HIỆN TẠI (Tom 21/10: Cú Đêm cũng phải tự
    # dọn, không giữ tin cũ) — tối đa 3 entry mới nhất VÀ không entry nào cũ
    # hơn entry mới nhất 12 giờ (1 chu kỳ đêm = 17:37 + 20:37 + 01:37).
    def trim_cudem(m):
        header, body = m.group(1), m.group(2)
        alerts = re.findall(r'(<div class="alert[^"]*">.*?</div>)\s*', body, re.S)
        if not alerts:
            return m.group(0)

        def _entry_dt(a):
            tm = re.search(r'<span class="t">(\d{1,2})/(\d{1,2}) (\d{1,2}):(\d{2}) PT', a)
            if not tm:
                return None
            d, mo, hh, mm = map(int, tm.groups())
            try:
                return datetime.datetime(now.year, mo, d, hh, mm)
            except ValueError:
                return None

        dts = [_entry_dt(a) for a in alerts]
        known = [x for x in dts if x]
        keep = alerts
        if known:
            newest = max(known)
            keep = [a for a, x in zip(alerts, dts)
                    if x is None or (newest - x).total_seconds() <= 12 * 3600]
        keep = keep[:3]
        if len(keep) != len(alerts):
            removed[0] += len(alerts) - len(keep)
            return header + '\n'.join(keep) + '\n</div>\n'
        return m.group(0)

    h = re.sub(r'(<div class="sec"[^>]*>\s*<h2>🌙 CÚ ĐÊM.*?</h2>)(.*?)(?=<div class="sec"|<div class="footer")',
               trim_cudem, h, flags=re.S)
    return h, removed[0]


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
    try:
        excluded = set(json.load(open(os.path.join(os.path.dirname(state_path), "zones.json"))).get("_excluded", []))
    except Exception:
        excluded = set()
    for t in ZONES:
        if t in excluded:
            new_state.pop(t, None)
            continue
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


BOOK_SEC = {"Sổ 1": "sec-longterm", "Sổ 2": "sec-so2", "Sổ 3": "sec-so3"}

def ensure_zone_rows(h, prices, state_path):
    """LƯỚI THÉP — Tom 19/10: mã có vùng (IN/BROKEN) mà thiếu dòng bảng thì TỰ TẠO dòng.
    Vá lỗ hổng COHR/ENTG/CRML/VLO: tín hiệu vùng bắn nhưng bảng không có dòng để xem."""
    try:
        state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    except Exception:
        state = {}
    created = []
    for t, z in ZONES.items():
        if t not in prices:
            continue
        st = (state.get(t) or {}).get("state")
        if st not in ("IN", "BROKEN"):
            continue
        if re.search(r"<b>" + re.escape(t) + r"</b></td>", h):
            continue
        px, pc = prices[t]
        lo, hi = z["entry"]
        sec = BOOK_SEC.get(z.get("book", ""), "sec-watch")
        dir_cls = "up" if pc >= 0 else "down"
        zone_txt = f"Vào ${lo:g}–${hi:g}"
        if z.get("targets"):
            t1, t2 = z["targets"]
            zone_txt += f" • Chốt ${t1:g}→${t2:g}"
        if z.get("cut"):
            zone_txt += f' • <span class="cut">Cắt dưới ${z["cut"]:g}</span>'
        if z.get("note"):
            zone_txt += f" — {z['note']}"
        row = (f'<tr><td><b>{t}</b></td><td>{fmt_price(px)} '
               f'<span class="{dir_cls}">{fmt_pct(pc)}</span></td>'
               f'<td><span class="zone">{zone_txt}</span></td>'
               f'<td><span class="badge {badge_class(st)}">{badge_text(st)}</span> '
               f'<span class="badge b-new">MỚI</span></td></tr>\n')
        pat = re.compile(r'(<div class="sec" id="' + sec + r'">.*?<table>.*?<tr><th>.*?</tr>)', re.S)
        h2, c = pat.subn(lambda m: m.group(1) + "\n    " + row, h, count=1)
        if c:
            h = h2
            created.append(t)
        else:
            print(f"  ! lưới thép: không tìm thấy section {sec} cho {t}", file=sys.stderr)
    if created:
        print(f"lưới thép: tự tạo dòng bảng cho {', '.join(created)}", flush=True)
    return h


def badge_class(st):
    return {"IN": "b-in", "BELOW": "b-near", "BROKEN": "b-cut", "ABOVE": "b-out"}[st]


def badge_text(st):
    return {"IN": "🟢 TRONG VÙNG", "BELOW": "🟡 DƯỚI VÙNG",
            "BROKEN": "🔴 GÃY VÙNG", "ABOVE": "⚪ TRÊN VÙNG"}[st]


DRAM_TICKERS = {"MU", "SNDK", "WDC", "STX"}


def add_dram_signals(h, events):
    """Tom lệnh 14/10: DRAM có dòng tín hiệu RIÊNG trong mục DRAM —
    mã nào chạm vùng mua/cắt/chốt là báo ngay tại chỗ, không lẫn tín hiệu chung."""
    dram_events = [e for e in events if e[0] in DRAM_TICKERS]
    if not dram_events:
        return h
    ts = now_pt().strftime("%H:%M")
    today = now_pt().strftime("%d/%m")
    block = ""
    for t, prev, st, px, pc in dram_events:
        z = ZONES[t]
        desc = {"IN": "mới vào vùng mua", "BELOW": "rớt khỏi vùng",
                "BROKEN": "THỦNG CẮT", "ABOVE": "vượt lên trên vùng"}[st]
        block += (f'  <div class="alert"><b>💾 {t} — {desc} {z["book"]}</b> — '
                  f'{fmt_price(px)} ({fmt_pct(pc)}) lúc {ts} PT.<br>'
                  f'<span class="t">Tín hiệu DRAM • {today} • giá intraday</span></div>\n')
    anchor = 'id="sec-dram"'
    i = h.find(anchor)
    if i == -1:
        return h
    j = h.find("</h2>", i) + len("</h2>")
    # xóa tín hiệu DRAM cũ trong mục (mỗi mã 1 dòng mới nhất)
    k = h.find("</table>", j)
    sec = h[j:k]
    seen = set()

    def keep_first(m):
        t = m.group(1)
        if t in seen:
            return ""
        seen.add(t)
        return m.group(0)

    sec = re.sub(r'<div class="alert"><b>💾 ([A-Z]{2,5})\b.*?</div>',
                 keep_first, block + sec, flags=re.S)
    return h[:j] + "\n" + sec + h[k:]


def add_signals(h, events, special=None):
    if not events:
        return h
    ts = now_pt().strftime("%H:%M")
    # 🎣 MỒI NGON — Tom 19/10: mồi dụ cá mập vào lưới thép (earnings/analyst).
    # CHÍNH XÁC 19/10 (~12:30): chỉ hiện mồi còn tươi — d trong [hôm nay−3, hôm nay+7].
    # Mồi hết hạn tự rụng, không dụ bằng mồi ôi.
    _bait_all = _fresh_bait_all()
    def _fresh_bait(t):
        return _bait_all.get(t, [])
    block = ""
    for t, prev, st, px, pc in events:
        z = ZONES[t]
        extra = ""
        if t == "MRVL" and z.get("pos"):
            pnl = (px - 266.5) * 2
            extra = f" Vị thế Tom ({z['pos']}): {pnl:+.0f}$ tổng."
        b = _fresh_bait(t)
        if b:
            extra += f" 🎣 Mồi: {'; '.join(b)}."
        # 💎 VÙNG ƯU ĐÃI ĐẶC BIỆT — Tom 19/10 (~13:30): tín hiệu đặc biệt cho deal đặc biệt
        is_special = st == "IN" and special and t in special
        if is_special:
            extra += " 💎 <b>VÙNG ƯU ĐÃI ĐẶC BIỆT</b> — discount ≥40% vs đỉnh 52w, deal ngon nhất lưới."
        desc = {"IN": "mới vào vùng", "BELOW": "rớt khỏi vùng",
                "BROKEN": "THỦNG CẮT", "ABOVE": "vượt lên trên vùng"}[st]
        cls = "alert special-deal" if is_special else "alert"
        block += (f'  <div class="{cls}"><b>🚨 {t} — {desc} {z["book"]}</b> — '
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
# Tom lệnh 15/10: bảng 51 mã quá hẹp, miss CRM/AAP/TGTX/WMT/TGT đang rebound.
# Vũ trụ mở rộng: quét thêm các mã Tom hay hỏi + mega-cap thanh khoản lớn.
# Không cần hàng trên bảng — vẫn bắt được mover/bounce/gap/RS.
NHIP_EXTRA = ["SKHY", "ASTS", "SMMT", "AMAT", "WDC",
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


def refresh_nhip(h, all_prices, spy_pc=None):
    """Xếp hạng lại ⚡ NHỊP THỊ TRƯỜNG (nóng/lạnh nhất) từ % cùng ngày.
    spy_pc: % của SPY -> dò 💪 KHỎE HƠN MARKET (mã xanh vượt SPY ≥3%: leader lúc thị trường yếu)."""
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
    # 💪 KHỎE HƠN MARKET — kỹ năng kiếm tiền #1 của pro: lúc market đỏ,
    # mã nào xanh và vượt SPY ≥3% là leader — tiền thông minh đang gom.
    if spy_pc is not None:
        rs = sorted(((t, pc - spy_pc) for t, (px, pc) in all_prices.items()
                     if isinstance(pc, (int, float)) and pc > 0 and (pc - spy_pc) >= 3.0),
                    key=lambda x: x[1], reverse=True)[:3]
        if rs:
            note += " 💪 Khỏe hơn market: " + ", ".join(f"{t} (+{d:.1f})" for t, d in rs)
    # 🎓 điểm signal 30 ngày (signal_score.py chấm cuối phiên) — script tự học
    try:
        ms_path = os.path.join(os.path.expanduser("~/workspace/market-board"),
                               "market_state.json")
        if os.path.exists(ms_path):
            ms = json.load(open(ms_path, encoding="utf-8"))
            sc = ms.get("signal_score", {})
            if sc.get("line"):
                note += f'<br><span class="t">{sc["line"]} • cập nhật {sc.get("updated", "")}</span>'
    except Exception:
        pass
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


# 🔄 ĐẢO CHIỀU XÁC NHẬN — Tom lệnh 15/10: "lỡ chuyến đò rồi, từ giờ canh bắt đáy,
# ăn cú rebound mạnh". Đáy xác nhận = đã rớt sâu (đáy ngày ≤-5%) + đang hồi mạnh
# (≥4% từ đáy) + có volume (≥1.5x TB) = tay to vào gom thật, không phải bull trap nhỏ.
# Đây là trigger bắt đáy, đi thẳng vào chat Tom ngay lập tức (không chờ bảng).
def detect_reversal(prices, daylows, repo_dir=None):
    cands = []
    for t, (px, pc) in prices.items():
        low = daylows.get(t)
        if not (low and low > 0):
            continue
        prev = px / (1 + pc / 100) if pc != -100 else px
        low_pc = (low - prev) / prev * 100 if prev else 0
        b = (px - low) / low * 100
        if low_pc <= -5.0 and b >= 4.0:
            cands.append(t)
    cands = cands[:25]
    revs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, r in ex.map(fetch_vol_ratio, cands):
            if r and r >= 1.5:
                px, pc = prices[t]
                low = daylows.get(t, 0)
                b = (px - low) / low * 100 if low else 0
                prev = px / (1 + pc / 100) if pc != -100 else px
                low_pc = (low - prev) / prev * 100 if prev else 0
                revs.append((t, low_pc, b, r, px, pc))
                if repo_dir:
                    journal_signal(repo_dir, "reversal", t, px,
                                   f"đáy {low_pc:+.1f}%, hồi +{b:.1f}%, vol {r}x")
    revs.sort(key=lambda x: x[2], reverse=True)
    return revs


# 😱→🛒 CAPITULATION DETECTOR — Tom lệnh 15/10: "khi mọi người hoảng loạn,
# là lúc mình gom hàng" (Buffett). Hoảng loạn thật = rớt sâu (≥6%) + volume tháo
# chạy (≥2.5x TB) + đã có tay to đỡ (ngóc ≥2% từ đáy). Đây là setup gom hàng,
# KHÔNG phải dự đoán đáy — cắt cứng vẫn bắt buộc.
def detect_capitulation(prices, daylows, repo_dir=None):
    cands = [t for t, (px, pc) in prices.items() if pc <= -6.0][:25]
    caps = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, r in ex.map(fetch_vol_ratio, cands):
            if r and r >= 2.5:
                px, pc = prices[t]
                low = daylows.get(t, 0)
                b = (px - low) / low * 100 if low else 0
                if b >= 2.0:
                    caps.append((t, pc, r, b, px))
                    if repo_dir:
                        journal_signal(repo_dir, "capitulation", t, px,
                                       f"{pc:+.1f}%, vol {r}x, ngóc +{b:.1f}%")
    caps.sort(key=lambda x: x[2], reverse=True)
    return caps


# 😨 VIX FEAR GAUGE — Tom lệnh 15/10: đo nỗi sợ market theo Buffett.
# VIX ≥30 hoảng loạn cực độ (lúc gom), 25–30 sợ hãi cao, 20–25 căng thẳng, <20 bình tĩnh.
def fear_gauge(h):
    try:
        _t, v = fetch_one("^VIX")
        vix = v[0] if v else 0
    except Exception:
        vix = 0
    if not vix:
        return h, "VIX: không lấy được", 0
    if vix >= 30:
        label, icon = "HOẢNG LOẠN CỰC ĐỘ — lúc gom hàng", "😱"
        vcls = "vix-red"
    elif vix >= 25:
        label, icon = "sợ hãi cao — canh gom", "😨"
        vcls = "vix-red"
    elif vix >= 20:
        label, icon = "căng thẳng", "😟"
        vcls = "vix-yellow"
    else:
        label, icon = "bình tĩnh", "😐"
        vcls = "vix-green"
    h = re.sub(r'<div class="note[^"]*" id="vix-gauge">.*?</div>', "", h)
    chk = now_pt().strftime("%H:%M")
    gauge = (f'<div class="note {vcls}" id="vix-gauge">{icon} <b>VIX {vix:.2f}</b> — {label} '
             f'<span class="t">kiểm tra {chk} PT • Buffett: tham lam khi người khác sợ hãi</span></div>\n')
    h2, c = re.subn(r'(<span class="updated">.*?</span>)', r"\g<1>\n" + gauge, h, count=1)
    if c:
        h = h2
    return h, f"VIX {vix:.1f} ({label})", vix


# 🧭 FEAR & GREED GAUGE — Tom lệnh 19/10 (Chiến lược sống còn): CNN Fear & Greed
# làm CỔNG SĂN cùng VIX. Chỉ số ngày, cập nhật trong phiên (không tick-by-tick) —
# hiển thị giờ cập nhật thật, không giả vờ real-time.
# Cổng săn MỞ khi: Fear&Greed ≤25 (extreme fear) hoặc VIX ≥25.
FG_API = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FG_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
         "Accept": "application/json",
         "Referer": "https://www.cnn.com/markets/fear-and-greed"}

def fg_rating(score):
    s = round(score)
    if s <= 25:
        return "EXTREME FEAR", "😱"
    if s <= 44:
        return "FEAR", "😨"
    if s <= 55:
        return "NEUTRAL", "😐"
    if s <= 74:
        return "GREED", "🤑"
    return "EXTREME GREED", "🤑"

def fg_tagline(rating):
    return {
        "EXTREME FEAR": "Đám đông hoảng loạn — lúc cá mập đi săn",
        "FEAR": "Đám đông sợ hãi — canh giăng lưới",
        "NEUTRAL": "Đám đông phân vân — đứng chờ",
        "GREED": "Đám đông tham lam — cẩn thận",
        "EXTREME GREED": "Đám đông say máu — cất lưới, giữ vốn",
    }.get(rating, "")
    s = round(score)
    if s <= 25:
        return "EXTREME FEAR", "😱"
    if s <= 44:
        return "FEAR", "😨"
    if s <= 55:
        return "NEUTRAL", "😐"
    if s <= 74:
        return "GREED", "🤑"
    return "EXTREME GREED", "🤑"

def fear_greed_gauge(h, vix, repo_dir):
    state_path = os.path.join(repo_dir, "fear_greed.json")
    old = {}
    try:
        old = json.load(open(state_path, encoding="utf-8"))
    except Exception:
        pass
    score, rating, ts_pt, prev_close, prev_week = (old.get("score"), old.get("rating"),
        old.get("ts_pt", "?"), old.get("prev_close"), old.get("prev_week"))
    try:
        req = Request(FG_API, headers=FG_UA)
        data = json.load(urlopen(req, timeout=15))
        fg = data["fear_and_greed"]
        score = round(float(fg["score"]), 1)
        rating, _icon = fg_rating(score)
        ts = datetime.fromisoformat(fg["timestamp"].replace("Z", "+00:00")).astimezone(PT)
        ts_pt = ts.strftime("%H:%M PT %d/%m")
        prev_close = round(float(fg.get("previous_close", 0)), 1)
        prev_week = round(float(fg.get("previous_1_week", 0)), 1)
        json.dump({"score": score, "rating": rating, "ts_pt": ts_pt,
                   "prev_close": prev_close, "prev_week": prev_week,
                   "gate_open": bool((score <= 25) or (vix and vix >= 25)),
                   "fetched_at": now_pt().isoformat()},
                  open(state_path, "w", encoding="utf-8"))
    except Exception as e:
        print(f"  ! fear&greed fetch fail ({e}), dùng giá trị cũ", file=sys.stderr)
    if score is None:
        return h, "F&G: chưa có dữ liệu"
    _r, icon = fg_rating(score)
    gate_open = (score <= 25) or (vix and vix >= 25)
    prev_gate = old.get("gate_open")
    # 🚨 Tín hiệu lật cổng — Tom 19/10: cổng MỞ/ĐÓNG phải báo, không chỉ hiển thị.
    if prev_gate is not None and bool(prev_gate) != gate_open:
        print(f"🚨 GATE FLIP: cổng săn {'MỞ' if gate_open else 'ĐÓNG'} "
              f"(F&G {score:.0f}, VIX {vix:.1f}) — báo Tom", flush=True)
    # 📓 Nhật ký cổng săn — Tom 19/10: đo hiệu quả chiến lược sống còn.
    # Mỗi lượt ghi 1 dòng: giờ, F&G, VIX, cổng mở/đóng → cuối tuần chấm điểm.
    try:
        with open(os.path.join(repo_dir, "strategy_gate_log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": now_pt().strftime("%Y-%m-%d %H:%M"), "fg": score,
                                "vix": round(vix, 1) if vix else None,
                                "gate_open": gate_open}) + "\n")
    except Exception as e:
        print(f"  ! gate log: {e}", file=sys.stderr)
    gate = ("🦈 <b>CỔNG SĂN MỞ</b> — cá mập đi bắt đáy" if gate_open
            else "🔒 <b>CỔNG SĂN ĐÓNG</b> — đứng ngoài, bảo quản vốn")
    h = re.sub(r'<div class="note[^"]*" id="fg-gauge">.*?</div>', "", h)
    gcls = "gate-open" if gate_open else ""
    chk = now_pt().strftime("%H:%M")
    gauge = (f'<div class="note {gcls}" id="fg-gauge">{icon} <b>Fear &amp; Greed {score:.1f}</b> — {rating} '
             f'<span class="t">{fg_tagline(rating)} • kiểm tra {chk} PT</span>'
             f'<br>{gate} <span class="t">Buffett: tham lam khi người khác sợ hãi</span></div>\n')
    h2, c = re.subn(r'(<div class="note[^"]*" id="vix-gauge">.*?</div>)', r"\g<1>\n" + gauge, h, count=1)
    if c:
        h = h2
    else:
        h2, c = re.subn(r'(<span class="updated">.*?</span>)', r"\g<1>\n" + gauge, h, count=1)
        if c:
            h = h2
    return h, f"F&G {score:.0f} ({rating}) — cổng săn {'MỞ' if gate_open else 'ĐÓNG'}"

# 🔪 SĂN CÁ MẬP — Tom 16/10: mục riêng cho ngày dump, xẻ thịt cá mập.
# Điền sóng hồi + capitulation + discount sâu vào <!-- SANCAM-AUTO --> mỗi lượt.
def sancam_update(h):
    if "<!-- SANCAM-AUTO -->" not in h:
        return h, "sancm: chưa có section"
    try:
        rs = json.load(open(os.path.join(REPO, "radar_state.json")))
        seen = rs.get("seen", {})
        today = now_pt().strftime("%Y-%m-%d")
        waves = []
        for k, ts in seen.items():
            if ":wave" in k and today in str(ts):
                t = k.split(":")[0]
                waves.append(t)
        waves = sorted(set(waves))[:8]
    except Exception:
        waves = []
    # discount sâu từ market_state
    try:
        ms = json.load(open(os.path.join(REPO, "market_state.json")))
        disc = ms.get("discounts", [])[:5]
    except Exception:
        disc = []
    parts = []
    if waves:
        parts.append('<div class="alert hot"><b>🌊 SÓNG HỒI ĐANG CHẠY</b> — ' +
                     ", ".join(f"<b>{t}</b>" for t in waves) +
                     "<br><span class='t'>Chế độ dump • ăn nhanh chốt nhanh • " +
                     now_pt().strftime("%H:%M") + " PT</span></div>")
    else:
        parts.append('<div class="alert"><b>🌊 Sóng hồi</b> — chưa có mã nào ngóc lên, radar đang quét 3 phút/lần.<br><span class="t">' +
                     now_pt().strftime("%H:%M") + " PT</span></div>")
    if disc:
        parts.append('<div class="alert"><b>🏷️ Hàng giảm giá sâu</b> — ' +
                     ", ".join(f"{d}" for d in disc) +
                     "<br><span class='t'>Discount vs đỉnh 52 tuần</span></div>")
    block = "\n".join(parts) + "\n"
    # Xóa block tự động cũ trong sec-sancm trước khi chèn mới (tránh đống).
    # BUG 20/10: regex cũ đòi alert dính sát header + không khoảng trắng đầu dòng
    # → không bao giờ khớp → alert "Sóng hồi" chồng 78 bản trên bảng.
    # Nay quét đoạn từ đầu section tới marker SANCAM-AUTO, xóa mọi alert tự
    # động cũ (chịu được khoảng trắng đầu dòng), chỉ giữ block mới nhất.
    _ms = h.find('id="sec-sancm"')
    _me = h.find("<!-- SANCAM-AUTO -->")
    if _ms != -1 and _me != -1 and _me > _ms:
        _seg = h[_ms:_me]

        def _drop_auto(_m):
            _t = _m.group(0).lower()
            return "" if ("sóng hồi" in _t or "hàng giảm giá sâu" in _t) else _m.group(0)

        _seg = re.sub(r'\s*<div class="alert[^"]*">.*?</div>', _drop_auto,
                      _seg, flags=re.S)
        h = h[:_ms] + _seg + h[_me:]
    h = h.replace("<!-- SANCAM-AUTO -->", block + "  <!-- SANCAM-AUTO -->", 1)
    # chỉ giữ 3 block mới nhất trong section này
    return h, f"sancm: {len(waves)} sóng hồi"



# 🕸️ LƯỚI THÉP — Tom lệnh 19/10 (~12:40): mã onsale 40–95% vs đỉnh 52w TỰ ĐỘNG vào lưới.
# Chỉ mã đạt chuẩn: có vùng duyệt (ZONES) hoặc mega-cap/ETF thanh khoản lớn.
# Mã chưa có vùng → "chờ vẽ vùng", watch-only, KHÔNG lên mâm (luật chất lượng 16/10).
STEEL_QUALITY = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    "TSM", "AMD", "NFLX", "CRM", "ORCL", "PLTR", "COIN", "SHOP", "UBER", "DIS",
    "BA", "NKE", "SBUX", "WMT", "TGT", "PANW", "CRWD", "ABNB", "MRNA", "MSTR",
    "BABA", "SPY", "QQQ", "VOO", "SMH", "XLE", "SCHD", "IBIT", "SOXL", "DIA",
    "IWM", "INTC", "DELL", "MU", "LRCX", "AMAT", "KLAC"}

def special_deal_set(prices, highs52):
    """💎 VÙNG ƯU ĐÃI ĐẶC BIỆT — Tom nhắc 19/10 (~13:30):
    mã MỚI VÀO vùng đã duyệt + discount ≥40% vs đỉnh 52w = deal đặc biệt, tín hiệu đặc biệt.
    (Mồi tươi hiển thị riêng trong alert khi có.)"""
    out = set()
    for t, (px, pc) in (prices or {}).items():
        h52 = (highs52 or {}).get(t)
        if not h52 or h52 <= 0:
            continue
        if (px - h52) / h52 * 100 <= -40:
            out.add(t)
    return out
    return out

def _fresh_bait_all():
    try:
        raw = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bait.json")))
    except Exception:
        raw = {}
    today = now_pt().date()
    out = {}
    for t, lst in raw.items():
        if t.startswith("_"):
            continue
        for e in (lst or []):
            try:
                d = datetime.strptime(e["d"], "%Y-%m-%d").date()
            except Exception:
                continue
            if -3 <= (d - today).days <= 7:
                out.setdefault(t, []).append(e["t"])
    return out

def steel_net_update(h, prices, highs52):
    """Dựng lại section LƯỚI THÉP mỗi lượt — idempotent."""
    # 19/10 ~12:55: tôn trọng _excluded — mã Tom đã xóa khỏi bảng không được chui lại qua lưới thép.
    try:
        _ex = set(json.load(open(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "zones.json"))).get("_excluded", []))
    except Exception:
        _ex = set()
    deals = []
    for t, (px, pc) in prices.items():
        if t in _ex:
            continue
        h52 = highs52.get(t)
        if not h52 or h52 <= 0:
            continue
        d = (px - h52) / h52 * 100
        if -95 <= d <= -40 and (t in ZONES or t in STEEL_QUALITY):
            deals.append((t, d, px, h52, pc))
    deals.sort(key=lambda x: x[1])
    bait = _fresh_bait_all()
    ts = now_pt().strftime("%H:%M")
    if deals:
        rows = []
        for t, d, px, h52, pc in deals:
            z = ZONES.get(t)
            if z:
                en = z.get("entry", [0, 0])
                tg = z.get("targets") or z.get("tp") or [0, 0]
                ct = z.get("cut")
                parts = [f'{z["book"]}: Vào ${en[0]:,.0f}–${en[1]:,.0f}']
                if tg[0]:
                    parts.append(f'Chốt ${tg[0]:,.0f}→${tg[1]:,.0f}')
                if ct:
                    parts.append(f'Cắt <${ct:,.0f}')
                zinfo = " · ".join(parts)
            else:
                zinfo = "chờ vẽ vùng (watch-only)"
            b = bait.get(t)
            brow = f"<br>🎣 Mồi: {'; '.join(b)}" if b else ""
            rows.append(
                f'<tr><td><b>{t}</b></td><td>{fmt_price(px)}</td>'
                f'<td style="color:#e05252"><b>{d:.0f}%</b></td>'
                f'<td>{fmt_price(h52)}</td><td>{zinfo}{brow}</td></tr>')
        body = ('<table class="tbl"><tr><th>Mã</th><th>Giá</th><th>Onsale</th>'
                '<th>Đỉnh 52w</th><th>Vùng / Mồi</th></tr>' +
                "".join(rows) + "</table>")
    else:
        body = '<div class="alert">Chưa có mã đạt chuẩn onsale 40–95% lúc này.</div>'
    block = (f'<div class="sec" id="sec-steelnet"><h2>🕸️ LƯỚI THÉP — ONSALE 40–95% (tự động)</h2>'
             f'<div class="note">Mã đạt chuẩn tự vào lưới mỗi lượt 5 phút • có vùng duyệt mới lên mâm • '
             f'mọi lệnh đều có cắt cứng • cập nhật {ts} PT</div>'
             f"<!-- STEELNET-AUTO -->{body}<!-- /STEELNET-AUTO --></div>\n")
    if 'id="sec-steelnet"' in h:
        h = re.sub(r'<div class="sec" id="sec-steelnet">.*?</div>\n',
                   lambda m: block, h, count=1, flags=re.S)
    else:
        anchor = '<div class="sec" id="sec-chinam">'
        h = h.replace(anchor, block + anchor, 1)
    # nav: thêm 🕸️ Lưới Thép sau 🔪 Săn CM (idempotent — chỉ khi chưa có)
    if 'href="#sec-steelnet"' not in h:
        h = re.sub(r'(href="#sec-sancm">🔪 Săn CM</a>)',
                   r'\1<a href="#sec-steelnet">🕸️ Lưới Thép</a>', h, count=1)
    return h, f"lưới thép: {len(deals)} mã onsale"
SHARK_FOCUS = [
    ("NVDA", "🦈🦈🦈", "Bullish premium $23.88M 12/10 (lớn nhất nhóm) · đơn TQ >$35B (Reuters 20/10), giá chưa bật"),
    ("TSM", "🦈🦈🦈", "$23.2M calendar spread $450 của desk pro (20/10) — gom dài hạn sau −11.7%/12 phiên"),
    ("GOOGL", "🦈🦈", "$14.2M + $5.3M bullish premium 12/10 — gom lặp lại nhiều phiên"),
    ("AVGO", "🦈🦈", "$7.49M bullish premium tổ chức 12/10 · call vol +34% TB"),
    ("INTC", "🦈", "$6.03M bullish premium 12/10 · OI call tăng vọt · JPM nâng UW→Neutral · CALL $100 exp 20/11 · earnings 29/10"),
]

SHARKFOCUS_WRAP = ('<div class="sharkfocus" style="border:3px solid #d4af37;'
                   'box-shadow:0 0 14px rgba(212,175,55,.55);'
                   'background:rgba(212,175,55,.13);border-radius:12px;'
                   'padding:10px 12px;margin:10px 0;overflow:hidden">')


def shark_focus_update(h, prices, pm=False):
    """👑 FOCUS CÁ MẬP GOM KHỦNG — gộp TRONG mục 🦈 CÁ MẬP GOM HÀNG (sec-urgent),
    ghim ngay dưới tiêu đề mục, TRONG KHUNG VÀNG. Tom lệnh 20/10 ~10:13 (gộp,
    bỏ section riêng chống trùng) + ~10:31 (chữ phải nằm trong khung vàng —
    bản bảng 5 cột tràn khung trên iPhone → đổi sang hàng xếp dọc gói gọn).
    Idempotent, marker-based (SHARKFOCUS-AUTO)."""
    ts = now_pt().strftime("%H:%M")
    zones = ZONES
    if not zones:
        try:
            raw = json.load(open(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "zones.json")))
            zones = {t: v for t, v in raw.items() if not t.startswith("_")}
        except Exception:
            zones = {}
    rows = []
    for t, tier, ev in SHARK_FOCUS:
        z = zones.get(t)
        if not z:
            continue
        en = z.get("entry", [0, 0])
        tg = z.get("targets") or z.get("tp") or [0, 0]
        ct = z.get("cut")
        pv = prices.get(t)
        if pv:
            px = pv[0]
            if ct and px < ct:
                st = "🔴 GÃY CẮT"
            elif en[0] <= px <= en[1]:
                st = "🟢 TRONG VÙNG"
            elif px > en[1] and px <= en[1] * 1.03:
                st = "🟡 SÁT TRÊN VÙNG"
            elif px < en[0] and px >= en[0] * 0.95:
                st = "🟡 SÁT DƯỚI VÙNG"
            elif px > en[1]:
                st = "⚪ TRÊN VÙNG"
            else:
                st = "⚪ DƯỚI VÙNG"
            pxs = fmt_price(px)
        else:
            st = "—"
            pxs = "—"
        zinfo = f'{z["book"]}: Vào ${en[0]:,.0f}–${en[1]:,.0f}'
        if tg and tg[0]:
            zinfo += f" · Chốt ${tg[0]:,.0f}→${tg[1]:,.0f}"
        if ct:
            zinfo += f" · Cắt <${ct:,.0f}"
        rows.append(
            '<div style="border-top:1px solid rgba(212,175,55,.45);'
            'padding:7px 0;word-break:break-word">'
            f"{tier} <b>{t}</b> {pxs} · {st}<br>"
            f'<span style="opacity:.92">{zinfo}</span><br>'
            f'<span style="opacity:.78;font-size:.92em">🧾 {ev}</span></div>')
    inner = (f'<div style="font-weight:700;font-size:1.06em">👑 FOCUS — CÁ MẬP ĐANG GOM KHỦNG (âm thầm trong hoảng loạn)</div>'
             f'<div class="note">Xếp theo độ khủng của bằng chứng dòng tiền lớn • giá + trạng thái vùng sống mỗi 5 phút • '
             f'tín hiệu canh riêng 09:37 + 11:37 PT • cổng săn chỉ là mùa, không phải điểm vào lệnh • cập nhật {ts} PT'
             + (' • giá premarket (thanh khoản mỏng)' if pm else '') + '</div>'
             + "".join(rows))
    # 1) Dọn section riêng cũ (sec-sharkfocus) + nav link của nó — đã gộp, chống trùng.
    if 'id="sec-sharkfocus"' in h:
        h = re.sub(r'<div class="sec" id="sec-sharkfocus">.*?</div>\n',
                   "", h, count=1, flags=re.S)
    h = re.sub(r'\s*<a href="#sec-sharkfocus">[^<]*</a>', "", h, count=1)
    # 2) Cập nhật tại chỗ nếu marker đã có (đồng thời nâng cấp khung vàng của
    # wrapper cũ); chưa có → ghim dưới tiêu đề sec-urgent.
    if "<!-- SHARKFOCUS-AUTO -->" in h:
        h = re.sub(r'<div class="sharkfocus" style="[^"]*">',
                   lambda m: SHARKFOCUS_WRAP, h, count=1)
        h = re.sub(r"<!-- SHARKFOCUS-AUTO -->.*?<!-- /SHARKFOCUS-AUTO -->",
                   lambda m: f"<!-- SHARKFOCUS-AUTO -->{inner}<!-- /SHARKFOCUS-AUTO -->",
                   h, count=1, flags=re.S)
    else:
        block = (SHARKFOCUS_WRAP +
                 f"<!-- SHARKFOCUS-AUTO -->{inner}<!-- /SHARKFOCUS-AUTO --></div>\n")
        anchor = ('<h2>🦈 CÁ MẬP GOM HÀNG — tiền lớn đang chạy '
                  '<span class="badge b-urgent">🦈 CÁ MẬP</span></h2>')
        if anchor in h:
            h = h.replace(anchor, anchor + "\n" + block, 1)
    return h, f"shark focus: {len(rows)} mã (gộp trong mục Cá Mập, khung vàng)"


def discount_card(h, prices, highs52):
    deals = []
    for t, (px, pc) in prices.items():
        h52 = highs52.get(t)
        if h52 and h52 > 0:
            d = (px - h52) / h52 * 100
            if d <= -20:
                deals.append((t, d, px))
    deals.sort(key=lambda x: x[1])
    top = deals[:6]
    if top:
        body = "<br>".join(f"🏷️ {t} <b>{d:.0f}%</b> so với đỉnh 52w (${px:,.2f})"
                           for t, d, px in top)
    else:
        body = "Chưa có mã giảm ≥20% so với đỉnh 52 tuần"
    card = (f'<div class="card" style="border-color:#1f6d4a">'
            f'<div class="lbl">🏷️ HÀNG GIẢM GIÁ — discount vs đỉnh 52w</div>'
            f'<div class="val" style="font-size:14px;line-height:1.9">{body}</div></div>')
    h = re.sub(r'<div class="card" style="border-color:#1f6d4a">.*?</div></div>',
               '', h, count=1, flags=re.S)
    h2, c = re.subn(r'(<div class="card" style="border-color:#8a6d1f">.*?</div></div>)',
                    r"\g<1>" + card, h, count=1, flags=re.S)
    if c:
        h = h2
    names = ", ".join(f"{t} {d:.0f}%" for t, d, _ in top) or "chưa có"
    return h, f"giảm giá: {names}"


# 🎓 ĐIỂM TIN CẬY SIGNAL — Tom lệnh 15/10 (~14:00): nâng bảng lên PRO.
# signal_score.py chấm mỗi chiều, ghi vào market_state.json. Alert khẩn gắn
# điểm loại tín hiệu: "🎓 đúng 82% (225/276 • 30 ngày)". Có số mẫu để trung thực.
TRUST = {}

def load_trust(repo_dir):
    global TRUST
    try:
        ms = json.load(open(os.path.join(repo_dir, "market_state.json")))
        TRUST = (ms.get("signal_score") or {}).get("by_type", {}) or {}
    except Exception:
        TRUST = {}

def trust_line(sig):
    d = TRUST.get(sig)
    if not d or not d.get("n"):
        return ""
    n = d["n"]; w = d["winrate"]
    wins = round(n * w / 100)
    return f"🎓 Đúng {w:.0f}% ({wins}/{n} • 30 ngày)"

# 🦈 TẦNG CÁ MẬP — Tom lệnh 15/10 (~14:00): xếp hạng tín hiệu cho dễ nhìn.
# 🦈🦈🦈 mạnh nhất (tiền lớn xác nhận) -> 🦈🦈 -> 🦈 bình thường.
TIER_MAP = {
    "UBS": ("🦈🦈🦈", "bounce"),       # bounce ≥8%
    "UB":  ("🦈🦈", "bounce"),        # bounce 5-8%
    "UR":  ("🦈", "urgent"),          # +4% trong ngày
    "UMF": ("🦈🦈", "money_flow"),    # dòng tiền vào
    "UCP": ("🦈🦈🦈", "capitulation"), # hoảng loạn + tay to đỡ
    "URV": ("🦈🦈🦈", "reversal"),     # đảo chiều xác nhận
    "UI":  ("🦈", None),              # vào vùng
}


# 💰 DÒNG TIỀN VÀO — Tom lệnh 15/10: script PRO phải bắt dòng tiền ngay lập tức.
# Volume hôm nay (annualized theo phần phiên đã trôi qua) so với trung bình 20 phiên.
# Chỉ quét mã đang có đà (bounce ≥3% / ngày ≥+2% / đỏ sâu đang ngóc) để kịp chu kỳ 5 phút.
def fetch_vol_ratio(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1mo"
        req = Request(url, headers=UA)
        with urlopen(req, timeout=10) as r:
            res = json.load(r)["chart"]["result"][0]
        vols = [v for v in res["indicators"]["quote"][0]["volume"] if v]
        if len(vols) < 22:
            return ticker, None
        avg = sum(vols[-21:-1]) / 20
        today = vols[-1]
        n = now_pt()
        el = (n.hour * 60 + n.minute - 390) / 390  # 6:30 -> 13:00 PT
        el = max(0.1, min(el, 1.0))
        ratio = (today / el) / avg if avg else 0
        return ticker, round(ratio, 1)
    except Exception:
        return ticker, None


def detect_money_flow(prices, daylows, repo_dir=None):
    """Trả về [(ticker, vol_x, bounce_%, day_%, price)] cho mã vol ≥2x TB đang có đà."""
    cands = []
    for t, (px, pc) in prices.items():
        low = daylows.get(t)
        b = (px - low) / low * 100 if low else 0
        if b >= 3.0 or pc >= 2.0 or (pc <= -4.0 and b >= 1.5):
            cands.append(t)
    cands = cands[:25]
    flows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, r in ex.map(fetch_vol_ratio, cands):
            if r and r >= 2.0:
                px, pc = prices[t]
                low = daylows.get(t, 0)
                b = (px - low) / low * 100 if low else 0
                flows.append((t, r, b, pc, px))
                if repo_dir:
                    journal_signal(repo_dir, "money_flow", t, px, f"vol {r}x, bounce +{b:.1f}%")
    flows.sort(key=lambda x: x[1], reverse=True)
    return flows


# 🚨 TÍN HIỆU KHẨN — Tom lệnh 15/10: "phải có tín hiệu chớp nháy khẩn, cho mọi người kiếm tiền".
# Chỉ signal mạnh nhất mới lên đây: bounce ≥5% từ đáy / tăng ≥4% trong ngày /
# vào vùng mới. Tối đa 3, dedup theo ngày — tránh chớp loạn gây nhiễu.
# PRO 15/10: thêm tầng 🔥 BOUNCE MẠNH (≥8% từ đáy) + 💰 DÒNG TIỀN VÀO (vol ≥2.5x).
URGENT_MAX = 3


def refresh_urgent(h, prices, daylows, events, repo_dir, flows=None, caps=None, revs=None):
    date_s = now_pt().strftime("%d/%m")
    # 19/10: mã trong _excluded (Tom xóa khỏi bảng) không được tạo alert khẩn
    try:
        excluded_u = set(json.load(open(os.path.join(repo_dir, "zones.json"))).get("_excluded", []))
    except Exception:
        excluded_u = set()
    cands = []  # (key, ticker, html)
    # 😱→🛒 CAPITULATION — hoảng loạn + tay to đỡ = lúc gom (Buffett) — ƯU TIÊN RENDER CAO NHẤT (🎯 BOTTOM SIGNAL, lệnh Tom 15/10)
    for t, pc, r, b, px in caps or []:
        if t in excluded_u:
            continue
        cands.append((f"UCP{t}", t,
                      f"😱→🛒 <b>{t} HOẢNG LOẠN {pc:+.1f}%, vol {r}x</b> — đã ngóc +{b:.1f}% từ đáy, tay to đang đỡ, canh gom"))
    # 🔄 ĐẢO CHIỀU XÁC NHẬN — trigger bắt đáy: rớt sâu + hồi mạnh + volume — ƯU TIÊN RENDER CAO NHẤT (🎯 BOTTOM SIGNAL, lệnh Tom 15/10)
    for t, low_pc, b, r, px, pc in revs or []:
        if t in excluded_u:
            continue
        cands.append((f"URV{t}", t,
                      f"🔄 <b>{t} ĐẢO CHIỀU XÁC NHẬN</b> — đáy {low_pc:+.1f}%, hồi +{b:.1f}%, vol {r}x. Bắt đáy có tay to đỡ"))
    for t, (px, pc) in prices.items():
        if t in excluded_u:
            continue
        low = daylows.get(t)
        b = (px - low) / low * 100 if low else 0
        if b >= 8.0:
            cands.append((f"UBS{t}", t,
                          f"🔥 <b>{t} BOUNCE MẠNH +{b:.1f}% từ đáy ngày</b> — tiền vào cực mạnh, canh đà"))
        elif b >= 5.0:
            cands.append((f"UB{t}", t,
                          f"🚨 <b>{t} bounce +{b:.1f}% từ đáy ngày</b> — tiền vào mạnh, canh đà"))
        elif pc >= 4.0:
            cands.append((f"UR{t}", t,
                          f"🚨 <b>{t} +{pc:.1f}% trong ngày</b> — đang chạy, canh vùng chốt"))
    # 💰 DÒNG TIỀN VÀO — vol ≥2.5x + đang bounce → tiền thật, ưu tiên khẩn
    for t, r, b, pc, px in flows or []:
        if t in excluded_u:
            continue
        if r >= 2.5 and b >= 3.0:
            cands.append((f"UMF{t}", t,
                          f"💰 <b>{t} DÒNG TIỀN VÀO</b> — vol {r}x TB, bounce +{b:.1f}% từ đáy"))
    for ev in events or []:
        t, _prev, st, px, _pc = ev[0], ev[1], ev[2], ev[3], ev[4]
        if t in excluded_u:
            continue
        if st == "IN":
            cands.append((f"UI{t}", t,
                          f"🚨 <b>{t} VÀO VÙNG</b> — ${px:,.2f}, vùng giá rẻ/đẹp"))
    # dedup + giới hạn
    fresh = []
    seen = set()
    for key, t, html in cands:
        mark = f"<!-- URG-{key}-{date_s} -->"
        if mark in h or key in seen:
            continue
        seen.add(key)
        fresh.append((mark, key, t, html))
        if len(fresh) >= URGENT_MAX:
            break
    if not fresh:
        return h, "khẩn: không có signal mới (giữ alert cũ)"
    for mark, key, t, html in fresh:
        px = prices.get(t, (0, 0))[0]
        journal_signal(repo_dir, "urgent", t, px, html[:60])
    ts_now = now_pt().isoformat(timespec="seconds")
    def tier_of(key):
        for pre in ("UBS", "UMF", "UCP", "URV", "UB", "UR", "UI"):
            if key.startswith(pre):
                return TIER_MAP[pre]
        return ("🦈", None)
    def build_alert(mark, key, t, html):
        # 🦈 tầng + 🎓 điểm tin cậy + ⏱️ độ tươi (JS tự cập nhật "cách đây X phút")
        # + data-ticker/data-sig-px cho máy đo "còn kịp" và 3 tính năng cá mập x3
        tier, sig = tier_of(key)
        trust = trust_line(sig) if sig else ""
        trust_html = f'<br><span class="trust">{trust}</span>' if trust else ""
        sig_px = prices.get(t, (0, 0))[0]
        return (
            f'{mark}<div class="alert urgent" data-ticker="{t}" data-sig-px="{sig_px:.2f}" data-ts="{ts_now}"><b>{tier} {html}</b><br>'
            f'<span class="t">🦈 Cá mập • {date_s} <span class="fresh"></span> • tiền lớn đang chạy • chỉ tham khảo</span>'
            f'{trust_html}</div>\n')
    new_alerts = "".join(build_alert(mark, key, t, html) for mark, key, t, html in fresh)
    # box đang là note trống -> thay note; đã có alert -> prepend, giữ alert cũ
    h2, c1 = re.subn(r'(<div id="urgentBox">)<div class="note">Chưa có tín hiệu khẩn.*?</div>',
                     r"\g<1>" + new_alerts, h, count=1, flags=re.S)
    if c1:
        h = h2
    else:
        h, c2 = re.subn(r'(<div id="urgentBox">)', r"\g<1>" + new_alerts,
                        h, count=1)
        if not c2:
            return h, "khẩn: thiếu #urgentBox"
    names = ", ".join(t for _, _, t, _ in fresh)
    return h, f"khẩn mới: {names}"


# 🔥 CÒI REBOUND TOÀN MARKET — Tom lệnh 15/10: "khi tất cả các mã và các sector
# có tín hiệu rebounce mạnh, phải báo khẩn nhanh, kiếm tiền".
# Điều kiện: bounce lan rộng (≥8 mã hồi ≥3% từ đáy ngày) + đa số sector xanh (≥8/13).
# Quét toàn vũ trụ 115+ mã (không chỉ 51 mã bảng). Báo KHẨN ngay vào #urgentBox,
# kèm top mã dẫn đầu để hành động. Watermark: chỉ báo lại khi số bounce tăng ≥3
# hoặc sang ngày mới — tránh chớp loạn.
def broad_rebound_klaxon(h, prices, daylows, sec_prices, repo_dir):
    date_s = now_pt().strftime("%d/%m")
    today = now_pt().strftime("%Y-%m-%d")
    bouncing = []
    prepping = []
    for t, (px, pc) in prices.items():
        low = daylows.get(t)
        if not (low and low > 0):
            continue
        b = (px - low) / low * 100
        if b >= 3.0:
            bouncing.append((t, b))
        elif pc <= -4.0 and b >= 1.5:
            prepping.append((t, b))
    bouncing.sort(key=lambda x: x[1], reverse=True)
    n_green = 0
    for _lbl, etf in SECTOR_CARDS:
        pc = sec_prices.get(etf, (0, 0))[1]
        if isinstance(pc, (int, float)) and pc > 0:
            n_green += 1
    n_b = len(bouncing)
    broad = (n_b >= 8 and n_green >= 8) or (n_b + len(prepping) >= 14 and n_green >= 8)
    if not broad:
        return h, f"rebound rộng: chưa đạt ({n_b} bounce, {n_green}/13 sector xanh)"
    sp = os.path.join(repo_dir, "broad_rebound.json")
    prev = {}
    try:
        if os.path.exists(sp):
            prev = json.load(open(sp, encoding="utf-8"))
    except Exception:
        prev = {}
    if prev.get("date") == today and n_b < prev.get("n_bounce", 0) + 3:
        return h, f"rebound rộng: đã báo hôm nay ({n_b} bounce)"
    leaders = ", ".join(f"{t} +{b:.1f}%" for t, b in bouncing[:5])
    html = (f"🔥 <b>REBOUND TOÀN MARKET</b> — {n_b} mã đang bounce ≥3% từ đáy, "
            f"{n_green}/13 sector xanh. Dẫn đầu: {leaders}. "
            f"Tiền đang chạy — canh đà, chỉ tham khảo kỹ thuật")
    mark = f"<!-- URG-BROAD-{today} -->"
    if mark in h:
        return h, "rebound rộng: đã có trên bảng"
    journal_signal(repo_dir, "broad_rebound", "MARKET", 0,
                   f"{n_b} bounce, {n_green}/13 xanh")
    new_alert = (f'{mark}<div class="alert urgent"><b>{html}</b><br>'
                 f'<span class="t">Còi rebound toàn market • {date_s} • '
                 f'chớp nhanh, tiền đang chạy • chỉ tham khảo</span></div>\n')
    h2, c1 = re.subn(r'(<div id="urgentBox">)<div class="note">Chưa có tín hiệu khẩn.*?</div>',
                     r"\g<1>" + new_alert, h, count=1, flags=re.S)
    if c1:
        h = h2
    else:
        h, c2 = re.subn(r'(<div id="urgentBox">)', r"\g<1>" + new_alert,
                        h, count=1)
        if not c2:
            return h, "rebound rộng: thiếu #urgentBox"
    json.dump({"date": today, "n_bounce": n_b, "n_green": n_green,
               "time": now_pt().isoformat()}, open(sp, "w"))
    return h, f"🔥 REBOUND TOÀN MARKET: {n_b} bounce, {n_green}/13 sector xanh"


# 🔄 ĐANG BOUNCE — Tom lệnh 15/10: "market rebound mạnh, phải bắt kịp signal".
# Hai tầng: ĐANG BOUNCE (hồi ≥3% từ đáy ngày, đã xác nhận) và CHUẨN BỊ BOUNCE
# (đỏ sâu ≥4% trong ngày + đã ngóc ≥1.5% từ đáy — tín hiệu sớm, KHÔNG phải dự đoán chắc chắn).
def refresh_bounce(h, prices, daylows, repo_dir=None, journal=True):
    bouncing, watching = [], []
    for t, (px, pc) in prices.items():
        low = daylows.get(t)
        if not (low and low > 0):
            continue
        b = (px - low) / low * 100
        if b >= 3.0:
            bouncing.append((t, b, px))
        elif pc <= -4.0 and b >= 1.5:
            watching.append((t, b, pc, px))
    bouncing.sort(key=lambda x: x[1], reverse=True)
    watching.sort(key=lambda x: x[1], reverse=True)
    top_b, top_w = bouncing[:4], watching[:4]
    parts = []
    if top_b:
        parts.append("<br>".join(f"🔄 {t} +{b:.1f}% từ đáy" for t, b, _ in top_b))
    if top_w:
        parts.append("<br>".join(f"🔶 {t} ngóc +{b:.1f}% từ đáy (hôm nay {pc:+.1f}%)"
                                 for t, b, pc, _ in top_w))
    body = "<br>".join(parts) if parts else "Chưa có mã hồi ≥3% từ đáy ngày"
    names = ", ".join(t for t, _, _ in top_b) or "chưa có"
    watch_names = ", ".join(t for t, _, _, _ in top_w) or "không"
    # 📓 ghi signal MỚI vào journal để học (so với trạng thái lượt trước)
    if journal and repo_dir:
        try:
            sp = os.path.join(repo_dir, "bounce_state.json")
            prev = {}
            if os.path.exists(sp):
                prev = json.load(open(sp, encoding="utf-8"))
            cur = {"b": [t for t, _, _ in bouncing], "w": [t for t, _, _, _ in watching]}
            for t, b, px in bouncing:
                if t not in prev.get("b", []) and t not in prev.get("w", []):
                    journal_signal(repo_dir, "bounce", t, px, f"+{b:.1f}% từ đáy")
            for t, b, pc, px in watching:
                if t not in prev.get("b", []) and t not in prev.get("w", []):
                    journal_signal(repo_dir, "bounce_watch", t, px,
                                   f"ngóc +{b:.1f}%, ngày {pc:+.1f}%")
            json.dump(cur, open(sp, "w", encoding="utf-8"))
        except Exception as e:
            print(f"  ! bounce journal: {e}", file=sys.stderr)
    card = (f'<div class="card" style="border-color:#8a6d1f">'
            f'<div class="lbl">🔄 BOUNCE / CHUẨN BỊ</div>'
            f'<div class="val up" style="font-size:14px;line-height:1.9">{body}</div></div>')
    anchor = "⚡ NHỊP THỊ TRƯỜNG"
    i = h.find(anchor)
    if i == -1:
        return h, "skip (không thấy NHỊP)"
    j = h.find('<div class="note">', i)
    if j == -1:
        return h, "skip (không thấy note)"
    sec = h[i:j]
    # dọn card bounce cũ (nếu có) để không trùng khi chạy lại
    sec = re.sub(r'<div class="card" style="border-color:#8a6d1f">.*?</div></div>',
                 '', sec, count=1, flags=re.S)
    # grid 3 cột (idempotent)
    sec = re.sub(r'grid-template-columns:1fr 1fr( 1fr)?',
                 'grid-template-columns:1fr 1fr 1fr', sec, count=1)
    # chèn card bounce mới sau card LẠNH NHẤT
    sec2, c2 = re.subn(
        r'(❄️ LẠNH NHẤT</div><div class="val down" style="font-size:14px;line-height:1.9">'
        r'.*?</div></div>)',
        r"\g<1>" + card, sec, count=1, flags=re.S)
    if c2 == 1:
        h = h[:i] + sec2 + h[j:]
        return h, f"bounce: {names} | chuẩn bị: {watch_names}"
    return h, "thiếu pattern card LẠNH"


# 📓 NHẬT KÝ SIGNAL — Tom lệnh 15/10: script phải HỌC, biết signal nào ăn tiền.
# Mỗi signal mới được ghi {thời điểm, mã, loại, giá}; cuối phiên signal_score.py
# chấm điểm (giá đóng cửa vs giá lúc báo) -> bảng điểm theo loại signal.
def journal_signal(repo_dir, sig_type, ticker, price, extra=""):
    try:
        p = os.path.join(repo_dir, "signal_journal.jsonl")
        rec = {"ts": now_pt().isoformat(), "date": now_pt().strftime("%Y-%m-%d"),
               "type": sig_type, "ticker": ticker,
               "price": round(float(price), 2), "extra": extra}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"  ! journal: {e}", file=sys.stderr)


# DÒNG TIỀN SECTOR — Tom lệnh 14/10: "tự động gắn cron cho tất cả sectors và nguồn tiền đi đâu".
# Mỗi lượt 5 phút lấy % của 6 ETF đại diện, xếp hạng lại BẢN ĐỒ SECTOR.
SECTOR_CARDS = [
    ("💾 Chip / Memory", "SOXX"),
    ("🛢️ Năng lượng", "XLE"),
    ("🏦 Ngân hàng", "XLF"),
    ("💻 Big Tech", "XLK"),
    ("🚀 Hàng không / Quốc phòng", "ITA"),
    ("💊 Dược / Healthcare", "XLV"),
    ("🛒 Tiêu dùng thiết yếu", "XLP"),
    ("🎯 Tiêu dùng không thiết yếu", "XLY"),
    ("🏭 Công nghiệp", "XLI"),
    ("⛏️ Vật liệu", "XLB"),
    ("💡 Tiện ích", "XLU"),
    ("🏠 Bất động sản", "XLRE"),
    ("📡 Viễn thông", "XLC"),
]


def refresh_sectors(h, sec_prices):
    """Làm tươi 🗺️ BẢN ĐỒ SECTOR mỗi lượt — tiền đang chạy đi đâu."""
    got = {etf: pc for etf, (px, pc) in sec_prices.items()
           if isinstance(pc, (int, float))}
    if len(got) < 4:
        return h, "skip (thiếu ETF)"
    date_s = now_pt().strftime("%d/%m")
    h, c0 = re.subn(r"🗺️ BẢN ĐỒ SECTOR — tiền đang chạy đi đâu \(\d{2}/\d{2}\)",
                    f"🗺️ BẢN ĐỒ SECTOR — tiền đang chạy đi đâu ({date_s})", h, count=1)
    n = c0
    for label, etf in SECTOR_CARDS:
        pc = got.get(etf)
        if pc is None:
            continue
        cls = "up" if pc > 0.3 else "down" if pc < -0.3 else "flat"
        border = ("#2c7a4b" if pc > 0.3 else "#7a2c2c" if pc < -0.3 else "#444")
        val = f"{etf} {fmt_pct(pc)}"
        # thay nội dung val + class + màu viền của đúng card này
        pat = (r'(<div class="card" style="border-color:#[0-9a-f]{6}"><div class="lbl">'
               + re.escape(label) + r'</div><div class="val )(down|up|flat)(" style="font-size:14px">).*?(</div>)')
        h2, c = re.subn(pat, r"\g<1>" + cls + r"\g<3>" + val + r"\g<4>", h, count=1, flags=re.S)
        if c == 0:  # card không có border-color (flat)
            pat2 = (r'(<div class="card"><div class="lbl">' + re.escape(label)
                    + r'</div><div class="val )(down|up|flat)(" style="font-size:14px">).*?(</div>)')
            h2, c = re.subn(pat2, r"\g<1>" + cls + r"\g<3>" + val + r"\g<4>", h, count=1, flags=re.S)
            if c:
                h2 = h2.replace(f'<div class="card"><div class="lbl">{label}</div>',
                                f'<div class="card" style="border-color:{border}"><div class="lbl">{label}</div>', 1)
        else:
            h2 = re.sub(r'(<div class="card" style="border-color:)[0-9a-f#]{7}("><div class="lbl">'
                        + re.escape(label) + r'</div>)',
                        r"\g<1>" + border + r"\g<2>", h2, count=1)
        h, n2 = h2, c
        n += n2
    # note dòng tiền dưới cùng
    ranked = sorted(got.items(), key=lambda x: x[1], reverse=True)
    best, best_pc = ranked[0]
    worst, worst_pc = ranked[-1]
    lbl_best = next((l for l, e in SECTOR_CARDS if e == best), best)
    lbl_worst = next((l for l, e in SECTOR_CARDS if e == worst), worst)
    note = (f"Tiền đang chạy: mạnh nhất {lbl_best} ({fmt_pct(best_pc)}), "
            f"yếu nhất {lbl_worst} ({fmt_pct(worst_pc)}). "
            f"Tự xếp hạng mỗi 5 phút từ ETF cùng ngày (Yahoo).")
    h, c1 = re.subn(r'(🗺️ BẢN ĐỒ SECTOR — tiền đang chạy đi đâu \(\d{2}/\d{2}\).*?</div>\s*<div class="note">).*?(</div>)',
                    r"\g<1>" + note + r"\g<2>", h, count=1, flags=re.S)
    return h, f"{n} card, mạnh nhất {best} {fmt_pct(best_pc)}"


FUTURES_IDX = ["ES=F", "NQ=F", "YM=F"]


def fetch_futures_pct():
    """% thay đổi futures chỉ số (ES/NQ/YM) so với đóng cửa phiên trước.
    Đóng cửa trước lấy từ nến NGÀY đã hoàn tất gần nhất: nếu nến cuối là phiên
    hôm nay (giờ ET) thì prev = close[-2], không thì close[-1]. TUYỆT ĐỐI
    không dùng meta chartPreviousClose — futures bị stale lùi 1 ngày
    (verify 22/10: meta ghi 7734.5 = đóng 19/10 trong khi đóng thật 21/10
    là 7800.25 → % sai lệch hẳn). Giá hiện tại = meta regularMarketPrice
    (futures chạy gần 24h nên luôn tươi)."""
    out = {}
    et = ZoneInfo("America/New_York")
    today_et = datetime.now(et).date()
    for t in FUTURES_IDX:
        try:
            url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
                   + t.replace("=", "%3D") + "?interval=1d&range=5d")
            req = Request(url, headers=UA)
            with urlopen(req, timeout=10) as r:
                d = json.load(r)["chart"]["result"][0]
            closes = [c for c in d["indicators"]["quote"][0]["close"] if c]
            price = float(d["meta"]["regularMarketPrice"])
            last_day = datetime.fromtimestamp(d["timestamp"][-1], et).date()
            if last_day == today_et and len(closes) >= 2:
                prev = closes[-2]
            else:
                prev = closes[-1]
            out[t] = round((price - prev) / prev * 100, 2)
        except Exception as e:
            print(f"  ! futures {t}: {e}", file=sys.stderr)
    return out


def fetch_pm_prices(tickers):
    """Giá premarket-aware: giá = nến 1m prepost gần nhất (fallback
    regularMarketPrice), % so với đóng cửa phiên trước lấy từ nến NGÀY đã
    hoàn tất (close[-2] nếu nến cuối là hôm nay ET) — không dùng meta
    chartPreviousClose (stale). Trả {ticker: (giá, %)} giống fetch_one.
    Dùng cho các khối 'sống' ngoài giờ phiên (FOCUS cá mập) — sửa lỗi
    22/10: ngoài giờ, fetch_one trả đóng cửa hôm trước nên trạng thái vùng
    trên bảng sai hẳn so với premarket thật."""
    et = ZoneInfo("America/New_York")
    today_et = datetime.now(et).date()

    def one(t):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1d&range=5d"
            with urlopen(Request(url, headers=UA), timeout=10) as r:
                d = json.load(r)["chart"]["result"][0]
            closes = [c for c in d["indicators"]["quote"][0]["close"] if c]
            last_day = datetime.fromtimestamp(d["timestamp"][-1], et).date()
            prev = closes[-2] if last_day == today_et and len(closes) >= 2 else closes[-1]
            px = None
            try:
                url2 = (f"https://query1.finance.yahoo.com/v8/finance/chart/{t}"
                        "?interval=1m&range=1d&includePrePost=true")
                with urlopen(Request(url2, headers=UA), timeout=10) as r:
                    m = json.load(r)["chart"]["result"][0]
                mc = [c for c in m["indicators"]["quote"][0]["close"] if c]
                if mc:
                    px = mc[-1]
            except Exception:
                pass
            if px is None:
                px = float(d["meta"]["regularMarketPrice"])
            return t, (round(float(px), 2), round((px - prev) / prev * 100, 2))
        except Exception as e:
            print(f"  ! pm {t}: {e}", file=sys.stderr)
            return t, None

    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for t, v in ex.map(one, tickers):
            if v:
                out[t] = v
    return out


def bull_bear_update(h, repo_dir):
    """⚔️ BULL vs BEAR — Tom hỏi 21/10: "BULL & BEAR, phe nào thắng".
    Điểm phe = (2*S&P% + Nasdaq% + Dow%)/4.
    >= +0.25: BULL thắng; <= -0.25: BEAR thắng; còn lại: ngang nhau.
    TRONG PHIÊN (06:30–13:00 PT): SPY/QQQ/DIA intraday.
    NGOÀI PHIÊN: futures ES/NQ/YM (chạy gần 24h) — sửa lỗi 22/10 Tom bắt:
    dải cũ dùng giá đóng cửa phiên trước nên premarket vẫn hiện verdict cũ
    trong khi futures đã rơi mạnh. Nhãn ghi rõ nguồn số.
    Dải verdict nằm ngay dưới badge cập nhật, tự làm mới mỗi lượt refresh."""
    n = now_pt()
    in_session = (n.weekday() < 5 and (n.hour, n.minute) >= (6, 30)
                  and n.hour < 13)
    if in_session:
        try:
            with ThreadPoolExecutor(max_workers=3) as ex:
                mkt = dict(ex.map(fetch_one, MARKET_ETFS))
        except Exception:
            return h
        mkt = {k: v for k, v in mkt.items() if v}
        if "SPY" not in mkt:
            return h
        a = mkt["SPY"][1]
        b = mkt.get("QQQ", (0, 0))[1]
        c = mkt.get("DIA", (0, 0))[1]
        labels = ("S&P 500", "Nasdaq", "Dow")
        src = "giá intraday"
    else:
        f = fetch_futures_pct()
        if "ES=F" not in f:
            return h
        a = f["ES=F"]
        b = f.get("NQ=F", 0)
        c = f.get("YM=F", 0)
        labels = ("S&P fut", "Nasdaq fut", "Dow fut")
        src = "theo futures (ngoài giờ chính)"
    score = (2 * a + b + c) / 4
    if score >= 0.25:
        verdict, emo, bg = "BULL THẮNG", "🐂", "rgba(22,163,74,.14)"
    elif score <= -0.25:
        verdict, emo, bg = "BEAR THẮNG", "🐻", "rgba(220,38,38,.14)"
    else:
        verdict, emo, bg = "NGANG NHAU", "⚖️", "rgba(148,163,184,.14)"
    strip = (f'<div class="bullbear" id="bullBear" style="border:2px solid #94a3b8;border-radius:12px;'
             f'padding:8px 12px;margin:8px 0;background:{bg};font-size:14px">'
             f'<b>⚔️ BULL vs BEAR — {emo} {verdict}</b> '
             f'<span style="color:#9fb3d9">{labels[0]} {fmt_pct(a)} • {labels[1]} {fmt_pct(b)} • {labels[2]} {fmt_pct(c)} • '
             f'điểm phe {score:+.2f} • {now_pt().strftime("%H:%M")} PT • {src}</span></div>\n')
    h = re.sub(r'<div class="bullbear" id="bullBear".*?</div>\n', "", h, flags=re.S)
    h = re.sub(r'(<span class="updated">.*?</span>)', r"\1\n" + strip, h, count=1, flags=re.S)
    return h


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
    spy_px, spy_pc = mkt["SPY"][:2]
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


def stamp_version(h, repo_dir):
    """Tom lệnh 15/10 (~12:30): bạn bè không nhận được update — cache kẹt.
    Mỗi lượt ghi version.json + đóng dấu meta board-v vào HTML.
    JS trên bảng fetch version.json mỗi 60s + khi tab hiện lại: thấy version
    mới là tự tải lại sạch. Không còn chuyện kẹt bản cũ."""
    import datetime
    v = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    try:
        json.dump({"v": v}, open(os.path.join(repo_dir, "version.json"), "w"))
    except Exception as e:
        print(f"  ! version.json: {e}", file=sys.stderr)
    if '<meta name="board-v"' in h:
        h = re.sub(r'<meta name="board-v" content="[^"]*">',
                   f'<meta name="board-v" content="{v}">', h, count=1)
    else:
        h = h.replace("</head>", f'<meta name="board-v" content="{v}">\n</head>', 1)
    return h, v


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
    # fix 16/10: chỉ add file đang tồn tại — back_to_wait_state.json vắng mặt làm
    # cả lệnh add fail (fatal pathspec), commit rỗng, push "Everything up-to-date"
    # rồi vẫn in "pushed" giả trong khi bảng chưa lên remote
    _files = ["index.html", "zone_state.json", "market_state.json",
              "quick_refresh.py", "refresh-tickers.txt", "zones.json", "fetch_failures.json",
              "version.json", "back_to_wait_state.json"]
    _files = [f for f in _files if os.path.exists(os.path.join(repo_dir, f))]
    run("git", "add", *_files)
    st = run("git", "status", "--porcelain").stdout.strip()
    if not st:
        return "nothing-to-commit"
    run("git", "-c", "user.email=tom@local", "-c", "user.name=Tom", "commit", "-qm", msg)
    for i in range(3):
        try:
            r = run("git", "push", "origin", "main")
        except subprocess.TimeoutExpired:
            print("  ! push timeout, thử lại", file=sys.stderr)
            time.sleep(30)
            continue
        if r.returncode == 0:
            return "pushed"
        time.sleep(30)
    return "push-failed"


def close_headers(h):
    """Chốt phiên 13:05 — đổi header intraday thành đóng cửa cho đúng ngày."""
    today = now_pt().strftime("%d/%m")
    h = h.replace("GIỮA PHIÊN", "ĐÓNG CỬA")
    h = h.replace("giữa phiên", "chốt phiên")
    h = re.sub(r"\(14/10 — giữa phiên sáng\)", f"({today} — chốt phiên)", h)
    h = re.sub(r"\(\d{2}/\d{2} — giữa phiên sáng\)", f"({today} — chốt phiên)", h)
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True)
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--mode", default="price", choices=["price", "full", "close"])
    a = ap.parse_args()

    if a.mode != "close" and not in_window():
        print("ngoài giờ giao dịch — thoát im lặng")
        return 0

    # NGOÀI PHIÊN CHÍNH (04:30–06:30 PT) → LITE: chỉ làm tươi Bull & Bear
    # (đọc futures) + fear gauge + badge. TUYỆT ĐỐI không chạy scanner
    # intraday (bounce/dòng tiền/capitulation/klaxon/shark): ngoài giờ, giá
    # Yahoo = đóng cửa hôm trước → scanner bắn tín hiệu "tươi" GIẢ từ dữ
    # liệu cũ (sự cố 22/10: banner REBOUND TOÀN MARKET giả giữa premarket
    # ngày dump). Giá trên bảng giữ nguyên số đóng cửa tới khi mở phiên.
    _hm = now_pt().hour * 60 + now_pt().minute
    if a.mode != "close" and not (6 * 60 + 30 <= _hm < 13 * 60):
        h = open(a.html, encoding="utf-8").read()
        h = ensure_gate_css(h)
        h, _nvix, _vix_num = fear_gauge(h)
        h, _nfg = fear_greed_gauge(h, _vix_num, a.repo_dir)
        # 👑 FOCUS cá mập cũng phải sống ngoài giờ (Tom bắt lỗi 22/10 ~06:25):
        # giá premarket-aware + trạng thái vùng tính lại theo giá premarket.
        load_zones(a.repo_dir)
        _pm = fetch_pm_prices([t for t, _, _ in SHARK_FOCUS])
        if _pm:
            h, _nfoc = shark_focus_update(h, _pm, pm=True)
            print(f"lite premarket: {_nfoc} (giá premarket)", flush=True)
        h = bull_bear_update(h, a.repo_dir)
        h = bump_badge(h, "premarket — Bull & Bear theo futures")
        ok, why = validate(h)
        if not ok:
            print(f"VALIDATE FAIL: {why}", file=sys.stderr)
            return 2
        open(a.html, "w", encoding="utf-8").write(h)
        repo_html = os.path.join(a.repo_dir, "index.html")
        if os.path.abspath(a.html) != os.path.abspath(repo_html):
            open(repo_html, "w", encoding="utf-8").write(h)
        h, _ = stamp_version(h, a.repo_dir)
        open(a.html, "w", encoding="utf-8").write(h)
        if os.path.abspath(a.html) != os.path.abspath(repo_html):
            open(repo_html, "w", encoding="utf-8").write(h)
        res = git_push(a.repo_dir,
                       f"refresh {now_pt().strftime('%d/%m %H:%M')} PT (lite premarket)")
        print(f"lite premarket: bull&bear + fear gauge tươi | git: {res}",
              flush=True)
        return 0 if res in ("pushed", "nothing-to-commit") else 3

    load_zones(a.repo_dir)

    tickers = discover_tickers(open(a.html, encoding="utf-8").read())
    print(f"quét thấy {len(tickers)} mã trên bảng", flush=True)
    print(f"lấy giá {len(tickers)} mã...", flush=True)
    prices, daylows, highs52 = fetch_prices(tickers)
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
        nhip_extra, nhip_lows, nhip_highs = fetch_prices(NHIP_EXTRA)
    except Exception as e:
        print(f"  ! nhip extra: {e}", file=sys.stderr)
        nhip_extra, nhip_lows = {}, {}
    # 💪 lấy % SPY để dò relative strength (mã khỏe hơn market)
    try:
        _mkt, _, _ = fetch_prices(["SPY"])
        spy_pc = _mkt.get("SPY", (0, 0))[1]
    except Exception:
        spy_pc = None
    h, nn = refresh_nhip(h, {**prices, **nhip_extra}, spy_pc)
    print(f"nhịp thị trường: {nn}", flush=True)

    # 🔄 ĐANG BOUNCE — Tom lệnh 15/10: market rebound phải bắt kịp signal.
    # Mã hồi ≥3% từ đáy ngày -> card riêng, không chờ vào vùng.
    h, nb = refresh_bounce(h, {**prices, **nhip_extra}, {**daylows, **nhip_lows},
                         a.repo_dir)
    print(f"bounce: {nb}", flush=True)

    # tự dọn mã trùng / chết — cho đúng Live
    h, cleaned = auto_cleanup(h, prices, tickers, a.repo_dir)
    if cleaned:
        print(f"tự dọn: {', '.join(cleaned)}", flush=True)

    # QUÉT MÃ CŨ KHÔNG ĐỦ ĐIỀU KIỆN — Tom lệnh 16/10: tự động xóa, không cần nhắc
    h, swept = sweep_stale_tickers(h, a.repo_dir)
    if swept:
        print(f"quét mã cũ: xóa {', '.join(swept)}", flush=True)
        # chạy lại auto_cleanup để xóa dòng của mã vừa bị loại
        h, cleaned2 = auto_cleanup(h, prices, tickers, a.repo_dir)
        if cleaned2:
            print(f"tự dọn: {', '.join(cleaned2)}", flush=True)

    # tự dọn TIN CŨ — Tom lệnh 15/10: tin cũ phải biến mất, xử tự động mỗi 5 phút
    h, n_pruned = prune_old_alerts(h)
    if n_pruned:
        print(f"dọn tin cũ: {n_pruned} tin", flush=True)

    # BẢN ĐỒ SECTOR — dòng tiền đi đâu, mỗi lượt
    try:
        sec_prices, _, _ = fetch_prices([etf for _, etf in SECTOR_CARDS])
    except Exception as e:
        print(f"  ! sector: {e}", file=sys.stderr)
        sec_prices = {}
    h, ns = refresh_sectors(h, sec_prices)
    print(f"bản đồ sector: {ns}", flush=True)

    tag = "giá"
    zone_events = None
    if a.mode in ("full", "close"):
        state_path = os.path.join(a.repo_dir, "zone_state.json")
        h, events = update_zone_badges(h, prices, state_path)
        # NOTE 21/10: ensure_zone_rows KHÔNG chạy ở đây nữa — nó chạy SAU steel_net_update
        # (cuối pipeline). Lý do: steel_net_update dựng lại lưới có thể RỚT mã đang IN
        # (vd STX vào vùng $695–720 thì discount chỉ còn -38.5% < ngưỡng 40%); ensure chạy
        # trước thấy dòng steelnet còn đó nên skip tạo dòng Sổ, rồi steelnet rớt mã →
        # mã IN mất dòng trên bảng (healthcheck NGUY HIỂM 06:50 21/10).
        zone_events = events
        if events:
            # 💎 VÙNG ƯU ĐÃI ĐẶC BIỆT — Tom 19/10 (~13:30): mã vào vùng + discount ≥40% + mồi tươi
            special = special_deal_set({**prices, **nhip_extra}, {**highs52, **nhip_highs})
            if special:
                print(f"💎 vùng ưu đãi đặc biệt: {sorted(special)}", flush=True)
            h = add_signals(h, events, special)
            h = add_dram_signals(h, events)
            tag = "tín hiệu vùng"
            print(f"tín hiệu vùng mới: {[(t, s) for t, _, s, _, _ in events]}", flush=True)

    # 💰 DÒNG TIỀN VÀO — Tom lệnh 15/10: script PRO bắt tiền thật ngay lập tức.
    # Quét vol spike trên toàn vũ trụ 115+ mã, chỉ mã đang có đà.
    all_px = {**prices, **nhip_extra}
    all_low = {**daylows, **nhip_lows}
    try:
        flows = detect_money_flow(all_px, all_low, a.repo_dir)
    except Exception as e:
        print(f"  ! money flow: {e}", file=sys.stderr)
        flows = []
    print(f"dòng tiền: {', '.join(f'{t} {r}x' for t, r, *_ in flows[:5]) or 'chưa có vol spike'}",
          flush=True)

    # 📡 RADAR WATCHLIST — Tom lệnh 15/10: signal chậm = đu đỉnh. Radar 3 phút
    # chỉ quét mã đang yếu (đỏ ≥3%) để bắt tier SỚM (+1.5% từ đáy), không chờ xác nhận.
    try:
        watch = [t for t, (px, pc) in all_px.items() if pc <= -3.0][:25]
        json.dump({"date": now_pt().strftime("%Y-%m-%d"),
                   "time": now_pt().isoformat(), "tickers": watch},
                  open(os.path.join(a.repo_dir, "radar_watchlist.json"), "w"))
    except Exception as e:
        print(f"  ! radar watchlist: {e}", file=sys.stderr)

    # 😱→🛒 CAPITULATION — Tom lệnh 15/10 (Buffett): hoảng loạn + tay to đỡ = lúc gom hàng
    try:
        caps = detect_capitulation(all_px, all_low, a.repo_dir)
    except Exception as e:
        print(f"  ! capitulation: {e}", file=sys.stderr)
        caps = []
    print(f"capitulation: {', '.join(f'{t} {r}x' for t, _, r, _, _ in caps) or 'chưa có'}",
          flush=True)

    # 🔄 ĐẢO CHIỀU XÁC NHẬN — Tom lệnh 15/10: lỡ chuyến đò sáng, từ giờ canh bắt đáy
    try:
        revs = detect_reversal(all_px, all_low, a.repo_dir)
    except Exception as e:
        print(f"  ! reversal: {e}", file=sys.stderr)
        revs = []
    print(f"đảo chiều: {', '.join(f'{t} +{b:.1f}%' for t, _, b, _, _, _ in revs) or 'chưa có'}",
          flush=True)
    if revs or caps:
        print(f"🎯 BOTTOM SIGNAL: {[t for t, *_ in revs] + [t for t, *_ in caps]} — báo chat Tom ngay",
              flush=True)

    # 😨 VIX FEAR GAUGE — thước đo nỗi sợ market
    h = ensure_gate_css(h)  # CSS chớp nháy cổng săn (idempotent)
    h, nvix, vix_num = fear_gauge(h)
    # 🧭 FEAR & GREED GAUGE — Tom 19/10 (Chiến lược sống còn): cổng săn cùng VIX
    h, nfg = fear_greed_gauge(h, vix_num, a.repo_dir)
    h, nsancm = sancam_update(h)
    print(f"fear gauge: {nvix} | {nfg}", flush=True)

    # 🏷️ HÀNG GIẢM GIÁ — shopping list lúc hoảng loạn (discount vs đỉnh 52w)
    h, nd = discount_card(h, prices, {**highs52, **nhip_highs})
    print(f"hàng giảm giá: {nd}", flush=True)

    # 🕸️ LƯỚI THÉP — Tom lệnh 19/10 (~12:40): mã đạt chuẩn onsale 40–95% TỰ ĐỘNG vào lưới
    h, nsteel = steel_net_update(h, {**prices, **nhip_extra}, {**highs52, **nhip_highs})
    print(f"lưới thép: {nsteel}", flush=True)

    # 🕸️ LƯỚI THÉP (bổ sung dòng còn thiếu) — CHẠY SAU steel_net_update (fix 21/10, xem NOTE ở trên):
    # mã IN/BROKEN mà steelnet vừa rớt (vd STX vào vùng nên discount <40%), hoặc chưa có
    # dòng ở bất kỳ đâu → tự tạo dòng ở mục Sổ đúng book. Không trùng (skip khi đã có dòng).
    if a.mode in ("full", "close"):
        h = ensure_zone_rows(h, prices, os.path.join(a.repo_dir, "zone_state.json"))

    # 🦈 CÁ MẬP GOM THẦM — Tom lệnh 20/10: focus 5 mã có bằng chứng gom âm thầm (dòng tiền lớn)
    h, nfoc = shark_focus_update(h, {**prices, **nhip_extra})
    print(f"shark focus: {nfoc}", flush=True)

    # 🚨 TÍN HIỆU KHẨN — Tom lệnh 15/10: chớp nháy khẩn cho mọi người kiếm tiền
    # PRO: quét toàn vũ trụ (không chỉ 51 mã bảng) + tầng BOUNCE MẠNH + DÒNG TIỀN + CAPITULATION + ĐẢO CHIỀU
    load_trust(a.repo_dir)  # 🎓 điểm tin cậy 30 ngày cho alert khẩn
    h, nu = refresh_urgent(h, all_px, all_low, zone_events, a.repo_dir, flows, caps, revs)
    print(f"tín hiệu khẩn: {nu}", flush=True)

    # 🔥 CÒI REBOUND TOÀN MARKET — Tom lệnh 15/10: bounce lan rộng + đa số sector
    # xanh thì báo KHẨN ngay, không chờ ai nhắc. Quét toàn vũ trụ 115+ mã.
    h, nbr = broad_rebound_klaxon(h, {**prices, **nhip_extra},
                                  {**daylows, **nhip_lows}, sec_prices, a.repo_dir)
    print(f"còi rebound rộng: {nbr}", flush=True)

    # mỗi mã một vị trong mục tín hiệu vùng + mã nóng (giữ cái mới nhất) —
    # chạy SAU khi chèn tín hiệu mới để trùng trong cùng lượt cũng biến mất
    h, c1 = dedup_section_alerts(h, "🚨 TÍN HIỆU VÙNG")
    h, c2 = dedup_section_alerts(h, "🔥 MÃ NÓNG")
    h, c3 = dedup_urgentbox(h)
    if c1 or c2 or c3:
        print(f"tự dọn trùng: {', '.join(c1 + c2 + c3)}", flush=True)

    # 🎯 GẮN VÙNG GIÁ + TP vào alert khẩn — Tom góp ý 15/10 (~13:00):
    # HOT feed clone alert nên tự có dòng Chốt/Cắt theo.
    h = attach_zone_tp(h)

    # 🦈 CÁ MẬP x3 — Tom duyệt 15/10 tối: ĐÃ VỀ ĐIỂM CHỜ + Sóng thứ mấy + Cá mập của ngày
    h, nwait = mark_back_to_wait(h, all_px, a.repo_dir)
    print(f"về điểm chờ: {nwait}", flush=True)
    h, nwave = mark_wave_count(h, a.repo_dir)
    print(f"sóng: {nwave}", flush=True)
    h, ntop = shark_of_day(h, a.repo_dir)
    print(f"cá mập của ngày: {ntop}", flush=True)

    if a.mode == "close":
        h = close_headers(h)
        tag = "chốt phiên"
        print("đã đổi header sang đóng cửa", flush=True)

    # còi báo động market — chạy mọi mode, không chờ ai nhắc
    h, regime = market_klaxon(h, a.repo_dir)
    if regime:
        tag = "còi market"
        print(f"market klaxon: {regime}", flush=True)

    # ⚔️ BULL vs BEAR — dải verdict phe thắng, tự cập nhật mỗi lượt (Tom hỏi 21/10)
    h = bull_bear_update(h, a.repo_dir)
    print("bull vs bear: đã cập nhật dải verdict", flush=True)

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

    # đóng dấu version để bảng tự phát hiện bản mới (fix kẹt cache — Tom 15/10)
    h, _ = stamp_version(h, a.repo_dir)
    open(a.html, "w", encoding="utf-8").write(h)
    if os.path.abspath(a.html) != os.path.abspath(repo_html):
        open(repo_html, "w", encoding="utf-8").write(h)

    msg = f"refresh {now_pt().strftime('%d/%m %H:%M')} PT ({tag})"
    res = git_push(a.repo_dir, msg)
    print("git:", res, flush=True)
    return 0 if res in ("pushed", "nothing-to-commit") else 3


if __name__ == "__main__":
    sys.exit(main())
