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
    # 01:00–13:00 PT (nới premarket 31/10 theo lệnh Tom "nối tiếp Á -> Mỹ":
    # phiên Á khép ~01:00 PT cũng là lúc premarket Mỹ mở 04:00 ET — làn
    # LITE chạy từ 01:00 để giá không đứt quãng; 04:30–13:00 như cũ, phần
    # 01:00–04:30 chỉ lite giá/futures, TUYỆT ĐỐI không scanner)
    return 1 * 60 <= hm <= 13 * 60


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


def fmt_price_vn(p):
    """$1.234,56 kiểu VN (dấu chấm nghìn, phẩy thập phân) — khớp khuôn dòng giá
    sec-ltalloc/sec-lt9. Giá <1000: $706,11."""
    s = f"{p:,.2f}"  # 1,234.56
    return "$" + s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_pct_vn(p):
    """−0,53% / +0,05% / 0,00% — 2 số lẻ, phẩy thập phân, dấu trừ Unicode −."""
    s = f"{abs(p):.2f}".replace(".", ",")
    return ("−" if p < 0 else "+" if p > 0 else "") + s + "%"


# Khuôn dòng giá "<b>TICKER</b> — $X (Y%)" — dùng ở sec-ltalloc + sec-lt9
# (ô kiểu SWING của mục LONG TERM). Hai updater cũ không phủ khuôn này:
# update_hot_entries chỉ ăn "<b>T ~$X (%)</b>", update_tables chỉ ăn hàng <tr>.
DASH_PRICE_PAT = re.compile(
    r"(<b>([A-Z]{2,6})</b>\s*—\s*)\$[\d.,]+(\s*\()[^)]*%(\))")


def update_dash_prices(h, prices):
    """<b>TICKER</b> — $X (Y%) -> giá + % ngày mới. Chỉ thay đúng số giá và số %
    trong ngoặc; tên quỹ, % vốn, span lãi lta-ret-* và mọi phần còn lại của
    dòng giữ nguyên. Ticker không có trong prices (fetch lỗi) -> giữ nguyên
    dòng, tuyệt đối không xóa. Idempotent: chạy lại với cùng prices = 0 đổi."""
    n = 0

    def rep(m):
        nonlocal n
        t = m.group(2)
        if t not in prices:
            return m.group(0)
        px, pc = prices[t]
        n += 1
        return (f"{m.group(1)}{fmt_price_vn(px)}{m.group(3)}"
                f"{fmt_pct_vn(pc)}{m.group(4)}")

    h = DASH_PRICE_PAT.sub(rep, h)
    return h, n


# === LẬT MÀU TÍN HIỆU LONG TERM THEO GIÁ REALTIME (Tom chốt 29/10 20:56) ===
# Span lta-zone-<MÃ> mang data-gom/data-cao (ltalloc_update ghi mỗi sáng).
# Mỗi lượt refresh, giá mới nhất của ô nào chạm dải nào thì ô đó tự đổi
# câu tín hiệu 3 trạng thái: 🟢 MUA ĐỊNH KỲ / 🔵 MUA NHIỀU THÊM / 🔴 BÁN BỚT 20%.
# Khuôn câu chữ KHỚP HỆT zone_text() trong ltalloc_update.py.
LT_SIG_PAT = re.compile(
    r'<span id="lta-zone-([A-Z]{2,6})"(?: class="lta-blink-[wrgb]")? '
    r'data-gom="([\d.]+)-([\d.]+)" '
    r'data-cao="([\d.]+)-([\d.]+)">(.*?)</span>(?=<br>)', re.S)
# FIX 30/10: lookahead </span>(?=<br>) — span zone luôn kết thúc ngay trước
# <br> trong ô quỹ; bản cũ dừng ở </span> lồng đầu tiên (chấm lta-dot) làm
# update_ltalloc_signals NHÂN ĐÔI câu tín hiệu mỗi lượt lật màu.


def _lt_signal_class(px, glo, ghi, tlo):
    """3 trạng thái LONG TERM (Tom chốt 30/10): px >= tlo → đỏ BÁN BỚT 20%
    (lta-blink-r); px < glo → xanh dương MUA NHIỀU THÊM (lta-blink-b);
    còn lại → xanh lá MUA ĐỊNH KỲ (lta-blink-g). Không còn trạng thái trắng."""
    if px >= tlo:
        return "lta-blink-r"
    if px < glo:
        return "lta-blink-b"
    return "lta-blink-g"


def _lt_money(x):
    s = fmt_price_vn(x)[1:]
    if s.endswith(",00"):
        s = s[:-3]
    elif "," in s and s.endswith("0"):
        s = s[:-1]
    return "$" + s


def _lt_pct1(x):
    return f"{x:.1f}".replace(".", ",") + "%"


def _lt_ruler_html(px, glo, tlo, thi):
    # Thước đo 3 vùng: lo = glo x0,90 ; hi = thi x1,02. KHỚP _ruler_html()
    # trong ltalloc_update.py.
    lo = glo * 0.90
    hi = thi * 1.02
    span = hi - lo
    wb = (glo - lo) / span * 100
    wg = (tlo - glo) / span * 100
    wr = (hi - tlo) / span * 100
    mp = min(max((px - lo) / span, 0.0), 1.0) * 100
    return (f'<div class="lta-ruler">'
            f'<div class="lta-seg-b" style="width:{wb:.1f}%"></div>'
            f'<div class="lta-seg-g" style="width:{wg:.1f}%"></div>'
            f'<div class="lta-seg-r" style="width:{wr:.1f}%"></div>'
            f'<div class="lta-mark" style="left:{mp:.1f}%">▼</div></div>')


def _lt_chip_html(t, px, glo, tlo):
    """KHUNG CHỚP ĐẦU HÀNG quỹ (Tom chốt 30/10 08:47): element riêng nằm
    TRÊN CÙNG mỗi hàng lta-row, trên mọi chi tiết — tên mã + tín hiệu +
    giá realtime + vùng, ai mở hàng là thấy đầu tiên.
    KHỚP HỆT _chip_html() trong ltalloc_update.py."""
    px_s = fmt_price_vn(px)
    glo_s, tlo_s = _lt_money(glo), _lt_money(tlo)
    if px >= tlo:
        cls, lab = "lta-chip-r", "🔴 BÁN BỚT 20%"
    elif px < glo:
        cls, lab = "lta-chip-b", "🔵 MUA NHIỀU THÊM"
    else:
        cls, lab = "lta-chip-g", "🟢 MUA ĐỊNH KỲ"
    return (f'<div class="lta-chip {cls}" id="lta-chip-{t}">'
            f'{t} · {lab} · Giá {px_s} · Vùng {glo_s}–{tlo_s}</div>')


def _lt_signal_text(t, px, glo, ghi, tlo, thi):
    """Nội dung span lta-zone (VÙNG CHI TIẾT, nằm dưới khung chớp đầu hàng):
    thước đo 3 vùng + dòng giá + khoảng cách tới màu kế tiếp.
    KHỚP HỆT _sig_text() trong ltalloc_update.py."""
    glo_s, tlo_s = _lt_money(glo), _lt_money(tlo)
    plan = (f'<span class="lta-zb">🔵 Mua nhiều: dưới {glo_s}</span> · '
            f'<span class="lta-zg">🟢 Định kỳ: {glo_s}–{tlo_s}</span> · '
            f'<span class="lta-zr">🔴 Bán 20%: trên {tlo_s}</span>')
    rt = (f"Mua ~{glo_s} → bán ~{tlo_s} = "
          f"+{_lt_pct1((tlo / glo - 1) * 100)} một vòng")
    ruler = _lt_ruler_html(px, glo, tlo, thi)
    if px >= tlo:
        dist = f"Rớt −{_lt_pct1((px - tlo) / px * 100)} về 🟢"
    elif px < glo:
        dist = f"Hồi +{_lt_pct1((glo - px) / px * 100)} về 🟢"
    else:
        dist = (f"Còn −{_lt_pct1((px - glo) / px * 100)} nữa sang 🔵 / "
                f"+{_lt_pct1((tlo - px) / px * 100)} nữa sang 🔴")
    return f"{ruler}{plan}<br>{rt}<br>{dist}"


def _lt_count_text(n_r, n_b, n_g):
    return f"📊 CẢ RỔ BÂY GIỜ: 🔴 {n_r} · 🔵 {n_b} · 🟢 {n_g}"


def _balanced_span_end(h, start):
    """Index ngay sau </span> ĐÓNG ĐÚNG của <span> mở tại start (stack-scan).
    GỐC RỄ vụ lặp dòng ltalloc (ảnh Tom 31/10: cặp dòng 'Mua ~.../Còn...'
    lặp ~40 lần trong ô VOO): LT_SIG_PAT cũ dừng ở </span> lồng đầu tiên
    có <br> theo sau (chính là span lta-zr) nên mỗi lượt refresh chỉ thay
    nửa đầu span, chừa đuôi cũ + thêm 1 cặp dòng mới. Mọi thay thế span
    lta-zone PHẢI đi qua hàm này."""
    depth, j = 0, start
    while True:
        o = h.find('<span', j)
        c = h.find('</span>', j)
        if c == -1:
            return len(h)
        if o != -1 and o < c:
            depth += 1
            j = o + 5
        else:
            depth -= 1
            j = c + 7
            if depth == 0:
                return j


_LTA_FOSSIL = re.compile(
    r'\s*<br>Mua ~[^<]*?một vòng<br>(?:Còn|Rớt) [^<]*?</span>')


def _strip_lta_fossils(h):
    """Dọn fossil ltalloc: các cặp dòng 'Mua ~.../Còn...' MỒ CÔI nằm NGOÀI
    span lta-zone — di tích lỗi thay-nửa-span trước 31/10/2026 mà các lần
    restore/clobber kéo theo nguyên vẹn (Tom bắt lỗi 02/11: 39 cặp/ô quỹ,
    mỗi cặp 1 giá realtime cũ khác nhau). Writer hiện tại thay span bằng
    stack-scan nên không tự dọn phần ngoài span; hàm này quét sau mỗi lượt
    update_ltalloc_signals để fossil không bao giờ tích lại được nữa."""
    pat = re.compile(r'<span id="lta-zone-([A-Z]{2,6})"[^>]*>')
    for m in reversed(list(pat.finditer(h))):
        end = _balanced_span_end(h, m.start())
        j = end
        while True:
            fm = _LTA_FOSSIL.match(h, j)
            if not fm:
                break
            j = fm.end()
        if j > end:
            h = h[:end] + h[j:]
    return h


def update_ltalloc_signals(h, prices):
    n = 0
    tally = {"lta-blink-r": 0, "lta-blink-b": 0, "lta-blink-g": 0}
    row_cls = {}
    chip_map = {}

    def rep(m):
        nonlocal n
        t = m.group(1)
        if t not in prices:
            return m.group(0)
        px = prices[t][0]
        glo, ghi, tlo, thi = (float(m.group(i)) for i in (2, 3, 4, 5))
        n += 1
        cls = _lt_signal_class(px, glo, ghi, tlo)
        tally[cls] += 1
        row_cls[t] = "lta-row-" + cls.rsplit("-", 1)[1]
        chip_map[t] = _lt_chip_html(t, px, glo, tlo)
        return (f'<span id="lta-zone-{t}" '
                f'data-gom="{m.group(2)}-{m.group(3)}" '
                f'data-cao="{m.group(4)}-{m.group(5)}">'
                f'{_lt_signal_text(t, px, glo, ghi, tlo, thi)}</span>')

    # Thay span lta-zone bằng stack-scan CÂN BẰNG (bản cũ LT_SIG_PAT.sub
    # dừng ở </span> lồng đầu tiên -> lặp dòng mỗi lượt, xem _balanced_span_end).
    _open_pat = re.compile(
        r'<span id="lta-zone-([A-Z]{2,6})"(?: class="lta-blink-[wrgb]")? '
        r'data-gom="([\d.]+)-([\d.]+)" data-cao="([\d.]+)-([\d.]+)">')
    _out, _i = [], 0
    while True:
        _m = _open_pat.search(h, _i)
        if not _m:
            _out.append(h[_i:])
            break
        _end = _balanced_span_end(h, _m.start())
        _out.append(h[_i:_m.start()])
        _out.append(rep(_m))
        _i = _end
    h = ''.join(_out)
    # Khung chớp ĐẦU HÀNG: thay element lta-chip-<T> theo giá realtime;
    # chưa có (bảng cũ) thì chèn ngay sau thẻ mở hàng lta-row-<T>.
    for t, chip in chip_map.items():
        cpat = re.compile(r'<div class="lta-chip[^"]*" id="lta-chip-'
                          + t + r'">.*?</div>', re.S)
        h, cn = cpat.subn(chip, h, count=1)
        if not cn:
            opat = re.compile(r'(<div class="alert lta-row[^"]*" id="lta-row-'
                              + t + r'">)')
            h = opat.sub(lambda mm: mm.group(1) + chip, h, count=1)
    # Cả hàng quỹ cũng lật màu theo cùng trạng thái (Tom chốt 30/10):
    # <div class="alert lta-row" id="lta-row-<T>"> mang class lta-row-g/b/r.
    for t, rc in row_cls.items():
        rpat = re.compile(r'(<div class="alert lta-row)(?: lta-row-[gbr])?'
                          r'(" id="lta-row-' + t + r'")')
        h = rpat.sub(lambda mm: mm.group(1) + " " + rc + mm.group(2),
                     h, count=1)
    if n:
        ct = _lt_count_text(tally["lta-blink-r"], tally["lta-blink-b"],
                            tally["lta-blink-g"])
        h = re.sub(r'(<span id="lta-count">)[^<]*(</span>)',
                   lambda mm: mm.group(1) + ct + mm.group(2), h, count=1)
    h = _strip_lta_fossils(h)
    return h, n


def _alert_ticker(full):
    """Mã của 1 khối alert: ưu tiên data-achip (khung đầu khối) →
    data-ticker → token HOA đầu tiên trong <b> đầu → token HOA đầu
    trong text. None nếu không tách được (alert không theo mã)."""
    m = re.search(r'data-achip="([A-Z]{2,6})"', full)
    if m:
        return m.group(1)
    m = re.search(r'data-ticker="([A-Z]{2,6})"', full)
    if m:
        return m.group(1)
    m = re.search(r"<b>(.*?)</b>", full, re.S)
    if m:
        mt = re.search(r"\b([A-Z]{2,6})\b", m.group(1))
        if mt:
            return mt.group(1)
    txt = re.sub(r"<[^>]+>", " ", full)
    mt = re.search(r"\b([A-Z]{2,6})\b", txt)
    return mt.group(1) if mt else None


def _alert_sig(full, ticker):
    """Chữ ký sự kiện của 1 alert: text đã bỏ thẻ, bỏ MÃ, bỏ mọi con số
    (giá/%/giờ/ngày) — 2 alert trùng THẬT (cùng sự kiện, chèn lại với số
    cập nhật) cho cùng chữ ký; 2 alert khác sự kiện của cùng 1 mã (strip
    daytrade + tín hiệu vùng, bounce + capitulation...) cho chữ ký khác
    nhau nên đều được giữ. BÀI HỌC 30/10 ~09:24: dedup theo mã đơn thuần
    đã xóa 61 alert hợp lệ khắp bảng trong 1 lượt refresh."""
    txt = re.sub(r"<[^>]+>", " ", full)
    if ticker:
        txt = txt.replace(ticker, " ")
    txt = re.sub(r"\$?\d[\d.,]*%?", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip().lower()
    return txt[:80]


def _sec_span_containing(h, pos):
    """(start, end) của mục <div class="sec"> chứa vị trí pos: từ thẻ mở
    .sec gần nhất ở trước pos tới thẻ mở .sec kế tiếp. BÀI HỌC 30/10:
    bản cũ tìm '<div class="sec">' (thừa dấu >) không bao giờ khớp thẻ
    thật có id → span tràn tới cuối file, dedup xuyên mục xóa alert của
    các mục sau."""
    starts = [m.start() for m in re.finditer(r'<div class="sec"[^>]*>', h)]
    s0 = None
    for s in starts:
        if s <= pos:
            s0 = s
        else:
            break
    if s0 is None:
        return None
    s1 = next((s for s in starts if s > s0), len(h))
    return s0, s1


def dedup_section_alerts(h, section_marker):
    """Dedup TRONG ĐÚNG 1 mục chứa marker: chỉ xóa alert trùng THẬT
    (cùng mã + cùng chữ ký sự kiện), giữ cái mới nhất (đầu tiên)."""
    i = h.find(section_marker)
    if i == -1:
        return h, []
    span = _sec_span_containing(h, i)
    if not span:
        return h, []
    s0, s1 = span
    j = h.find("</h2>", i) + len("</h2>")
    if j < s0 or j > s1:
        j = s0
    sec = h[j:s1]
    seen = set()
    removed = []

    def keep_first(full, inner):
        t = _alert_ticker(full)
        key = (t, _alert_sig(full, t))
        if key in seen:
            removed.append(f"{t}(alert trùng)")
            return ""
        seen.add(key)
        return full

    sec2 = sub_balanced_alerts(sec, keep_first)
    return h[:j] + sec2 + h[s1:], removed


def dedup_alert_section(h, sec_id):
    """Xóa alert trùng THẬT trong 1 mục (cùng mã + cùng chữ ký sự kiện —
    Tom bắt lỗi trùng 30/10: cùng alert lặp 2–3 lần trong swing/daytrade/
    dram). Alert khác sự kiện của cùng mã thì GIỮ (bài học 09:24 30/10:
    dedup theo mã đơn thuần xóa cả strip daytrade hợp lệ). Quét cân bằng,
    an toàn với khung zchip."""
    m = re.search(r'<div class="sec" id="' + sec_id + r'"[^>]*>', h)
    if not m:
        return h, []
    start = m.start()
    nxt = h.find('<div class="sec"', start + 10)
    end = nxt if nxt != -1 else len(h)
    seg = h[start:end]
    seen = set()
    removed = []

    def keep_first(full, inner):
        t = _alert_ticker(full)
        key = (t, _alert_sig(full, t))
        if key in seen:
            removed.append(t)
            return ""
        seen.add(key)
        return full

    seg2 = sub_balanced_alerts(seg, keep_first)
    return h[:start] + seg2 + h[end:], removed


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

    # 30/10: quét CÂN BẰNG — regex non-greedy cũ dừng ở </div> đầu tiên
    # (chính là thẻ đóng của KHUNG zchip trong alert) nên khi dedup quyết
    # xóa 1 alert trùng, nó chỉ xóa được phần đầu + khung, để lại phần
    # thân alert mồ côi trên bảng. Mã đọc qua _alert_ticker (khung trước).
    def keep_first(full, inner):
        if 'class="alert urgent"' not in full.split(">", 1)[0]:
            return full
        t = _alert_ticker(full)
        # 30/10: dedup theo (mã + chữ ký sự kiện) — KHÔNG theo mã đơn thuần:
        # capitulation/moneyflow/reversal của cùng 1 mã là các sự kiện KHÁC
        # nhau, phải giữ cả; chỉ alert chèn lại cùng sự kiện mới là trùng.
        key = (t, _alert_sig(full, t))
        if key in seen_tk:
            removed.append(f"{t}(khẩn trùng)")
            return ""
        seen_tk.add(key)
        return full

    box2 = sub_balanced_alerts(box, keep_first)
    return h[:j] + box2 + h[k:], removed


STALE_STATUS_PAT = re.compile(
    r'\s*—\s*[🟢🟡🔴⚪⏸️]+\s*'
    r'(?:TRONG VÙNG|DƯỚI VÙNG|TRÊN VÙNG|QUANH MÉP(?:\s+TRÊN|\s+DƯỚI)?\s+VÙNG|'
    r'GÃY VÙNG(?:\s+—\s+THỦNG CẮT)?|SÁT\s+(?:TRÊN|DƯỚI)\s+VÙNG)'
    r'\s*\$[\d,]+–\$?[\d,]+'
    r'(?:\s*·\s*Cắt\s*&lt;\$[\d,]+)?')


def clean_shark_layers(h):
    """DỌN LỚP MỤC 🦈 CÁ MẬP + CÂU TRẠNG THÁI CŨ (Tom bắt lỗi 2/11 ~19:28:
    mục cá mập trùng lặp triền miên + trạng thái/giá không khớp Yahoo).
    3 gốc rễ, sửa 1 lần cho mọi lượt sau:
    (1) Alert tin thường (class 'alert') của mã đã có khối 👑 FOCUS trong
        cùng mục → 1 mã hiện 2 lần. FOCUS là khối chuẩn của mã đó (Tom
        chốt 20/10) → bỏ bản alert thường; GIỮ NGUYÊN 'alert urgent'
        (sự kiện tín hiệu thật, không phải bản tin lặp).
    (2) Hóa thạch: alert mang ngày cũ trong <span class="t"> (vd WDC
        22/10) được chip gắn giá mới mỗi lượt nên trông như tin mới và
        thoát mọi vòng dọn theo tuổi → xóa khi ngày đầu trong span.t
        khác hôm nay. Chỉ áp cho alert thường, không đụng alert urgent.
    (3) Câu trạng thái CŨ trong thân alert ('— 🟢 TRONG VÙNG $205–218'
        ghi lúc 08:40) nằm cạnh chip MỚI (giá Yahoo hiện tại) → 1 khối
        2 trạng thái ngược nhau. Alert đã có chip (data-achip) thì chip
        là nguồn số DUY NHẤT → cắt câu trạng thái cũ, giữ phần tin.
    Idempotent — chạy cuối pipeline, sau update_alert_chips."""
    notes = []
    today = now_pt()

    def _strip(full, inner):
        if 'data-achip="' not in full:
            return full
        new = STALE_STATUS_PAT.sub("", full)
        if new != full:
            notes.append("bỏ câu trạng thái cũ")
        return new

    for sec in ALERT_CHIP_SECS:
        m = re.search(r'<div class="sec" id="' + sec + r'"[^>]*>', h)
        if not m:
            continue
        s0 = m.start()
        nxt = h.find('<div class="sec"', s0 + 10)
        s1 = nxt if nxt != -1 else len(h)
        h = h[:s0] + sub_balanced_alerts(h[s0:s1], _strip) + h[s1:]

    m = re.search(r'<div class="sec" id="sec-urgent"[^>]*>', h)
    if m:
        s0 = m.start()
        nxt = h.find('<div class="sec"', s0 + 10)
        s1 = nxt if nxt != -1 else len(h)
        focus_tks = {t for t, _tier, _ev in SHARK_FOCUS}

        def _drop(full, inner):
            cls = full.split(">", 1)[0]
            if 'alert urgent' in cls or 'sharkfocus' in full \
                    or 'sharkday' in full:
                return full
            mt = re.search(r'<span class="t">(.*?)</span>', full, re.S)
            if mt:
                dates = re.findall(r'(\d{1,2})/(\d{1,2})', mt.group(1))
                if dates:
                    d0, m0 = int(dates[0][0]), int(dates[0][1])
                    if (m0, d0) != (today.month, today.day):
                        notes.append(
                            f"hóa thạch {_alert_ticker(full) or '?'} {d0}/{m0}")
                        return ""
            t = _alert_ticker(full)
            if t in focus_tks:
                notes.append(f"trùng FOCUS {t}")
                return ""
            return full

        h = h[:s0] + sub_balanced_alerts(h[s0:s1], _drop) + h[s1:]
    return h, notes


def build_hot_feed(h, prices):
    """Dựng TĨNH nội dung #hotFeed (Tom bắt lỗi 30/10: mục HOT trên trang
    riêng trống trơn — bản cũ chỉ có JS buildHotFeed chạy trên bảng
    chính, trang riêng trích HTML tĩnh nên #hotFeed mãi kẹt ở dòng chờ
    'Đang gom tin hot…'). Cùng luật chọn như JS: (1) headline của alert
    gắn HOT (class hot / badge b-hot) khắp bảng, trừ sec-hot + sec-cudem;
    (2) mã chạy ≥|3%| trong phiên từ prices (giá, %) — top 5 theo |%|.
    Bảng chính vẫn được JS làm tươi thêm khi mở; trang riêng đọc bản
    tĩnh này là có mã ngay."""
    m = re.search(r'<div id="hotFeed">', h)
    if not m:
        return h, 0
    depth, j = 0, m.start()
    end = None
    while True:
        o = h.find('<div', j)
        c = h.find('</div>', j)
        if c == -1:
            return h, 0
        if o != -1 and o < c:
            depth += 1
            j = o + 4
        else:
            depth -= 1
            j = c + 6
            if depth == 0:
                end = j
                break
    entries = []
    seen = set()
    sec_starts = [(mm.start(), mm.group(1)) for mm in
                  re.finditer(r'<div class="sec" id="(sec-[a-z0-9]+)"', h)]
    for idx, (s0, sid) in enumerate(sec_starts):
        if sid in ("sec-hot", "sec-cudem"):
            continue
        s1 = sec_starts[idx + 1][0] if idx + 1 < len(sec_starts) else len(h)
        seg = h[s0:s1]
        hm = re.search(r'<h2>(.*?)</h2>', seg, re.S)
        sname = re.sub(r'<[^>]+>', '', hm.group(1)).strip() if hm else sid

        def _grab(full, inner, _sname=sname):
            open_tag = full.split('>', 1)[0]
            if 'hot' in open_tag or 'b-hot' in full:
                bm = re.search(r'<b>(.*?)</b>', full, re.S)
                if bm:
                    txt = re.sub(r'<[^>]+>', '', bm.group(1)).strip()
                    key = txt[:60]
                    if txt and key not in seen:
                        seen.add(key)
                        entries.append(
                            '<div class="alert hot" style="padding:8px 10px">'
                            f'🔥 <b>{txt[:90]}</b> '
                            '<span style="font-size:12px;color:#9fc5ff">'
                            f'— ở mục {_sname}</span></div>')
            return full

        sub_balanced_alerts(seg, _grab)
    movers = []
    for t, v in prices.items():
        try:
            pct = v[1]
        except Exception:
            continue
        if pct is not None and abs(pct) >= 3:
            movers.append((t, pct))
    movers.sort(key=lambda x: -abs(x[1]))
    now = now_pt()
    for t, pct in movers[:5]:
        cls = 'up' if pct > 0 else 'down'
        entries.append(
            f'<div class="alert hot"><b>🔥 {t} chạy <span class="{cls}">'
            f'{pct:+.1f}%</span></b> trong phiên — mã nóng, canh vùng.<br>'
            f'<span class="t">Giá trực tiếp • cập nhật '
            f'{now.strftime("%H:%M")} PT {now.strftime("%d/%m")}</span></div>')
    inner = ''.join(entries) if entries else \
        '<div class="note">Chưa có tin/mã HOT nào lúc này.</div>'
    return h[:m.end()] + inner + h[end - 6:], len(entries)


LIVE_STRIP_SYMS = ['SPY', 'QQQ', 'NVDA', 'TSM', 'MU', 'MRVL', 'RKLB', 'DRAM']


def _fmt_px(n):
    return f"{n:,.0f}" if n >= 1000 else f"{n:.2f}"


def build_live_strip(h, prices):
    """Dung TINH #liveStrip — cung benh voi #hotFeed 30/10: JS chi chay tren
    bang chinh (fetch Yahoo qua proxy), trang rieng muc.html?s=sec-live chi
    trich HTML tinh nen ket 'Dang tai gia live...' vinh vien (Tom kiem tra
    1/11: 26/27 muc OK, rieng LIVE loi). Chip theo dung format JS: ma, gia,
    % so voi dong cua hom truoc; |%| >= 3 gan HOT."""
    m = re.search(r'<div id="liveStrip"[^>]*>', h)
    if not m:
        return h, 0
    depth, j, end = 0, m.start(), None
    while True:
        o = h.find('<div', j)
        c = h.find('</div>', j)
        if c == -1:
            return h, 0
        if o != -1 and o < c:
            depth += 1
            j = o + 4
        else:
            depth -= 1
            j = c + 6
            if depth == 0:
                end = j
                break
    chips = []
    for sym in LIVE_STRIP_SYMS:
        v = prices.get(sym)
        if not v:
            continue
        try:
            px, pct = float(v[0]), float(v[1])
        except Exception:
            continue
        cls = 'up' if pct > 0.05 else ('down' if pct < -0.05 else 'flat')
        arrow = '▲' if pct > 0.05 else ('▼' if pct < -0.05 else '●')
        hot = ' <span class="badge b-hot">🔥 HOT</span>' if abs(pct) >= 3 else ''
        chips.append(
            f'<div class="livechip"><div class="lt">{sym}{hot}</div>'
            f'<div class="lp">{_fmt_px(px)}</div>'
            f'<div class="lc {cls}">{arrow} {abs(pct):.2f}%</div></div>')
    if not chips:
        return h, 0
    return h[:m.end()] + ''.join(chips) + h[end - 6:], len(chips)


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

    def add_tp(full, inner):
        if 'class="alert urgent"' not in full.split(">", 1)[0]:
            return full
        # Alert đã có KHUNG ĐẦU KHỐI (zchip) thì khung đã ghi Chốt/Cắt —
        # gắn thêm dòng 🎯 là trùng lặp (Tom bắt lỗi 30/10).
        if "🎯" in inner or "data-achip" in full:
            return full
        t = _alert_ticker(full)
        if not t:
            return full
        line = zone_tp_line(t)
        if not line:
            return full
        return full[:-len("</div>")] + \
            f'<br><span class="zt">{line}</span></div>'

    box2 = sub_balanced_alerts(box, add_tp)
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
        # 2/11: dòng tóm tắt chỉ giữ NHÃN sự kiện (trước dấu — đầu tiên) —
        # bản cũ chép nguyên headline nên mã hiện 2 lần trong cùng mục
        # (1 ở dòng Cá Mập Của Ngày, 1 ở alert gốc ngay dưới).
        why = re.sub(r"^🦈+\s*", "", why.split(" — ")[0]).strip()
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


def sub_balanced_alerts(h, fn):
    """Thay thế từng khối <div class="alert...">...</div> CÂN BẰNG bằng fn(full, inner).

    GỐC RỄ vụ kẹt VALIDATE FAIL từ 12:48 29/10: regex non-greedy
    `<div class="alert[^"]*">(.*?)</div>` dừng ở thẻ </div> ĐẦU TIÊN nên CẮT ĐÔI
    alert chứa div lồng nhau (ô ltalloc: thanh % = 2 div lồng trong alert).
    Hàm xóa sau đó ăn mất 3 thẻ mở mà chỉ trừ 1 thẻ đóng -> mỗi ô lệch -2,
    13 ô = -26, validate 'thẻ div lệch' chặn mọi lượt ghi/push.
    Hàm này dùng stack-scan nên khối cân bằng luôn được xử nguyên vẹn; vùng
    <script> (JS chứa literal '<div class="alert...') được bỏ qua nguyên trạng."""
    script_spans = [(m.start(), m.end())
                    for m in re.finditer(r'<script[^>]*>.*?</script>', h, re.S)]

    def _in_script(pos):
        return any(s <= pos < e for s, e in script_spans)

    pat = re.compile(r'<div class="alert[^>]*>')
    out, i = [], 0
    while True:
        m = pat.search(h, i)
        if not m:
            out.append(h[i:])
            break
        if _in_script(m.start()):
            e = next(e for s, e in script_spans if s <= m.start() < e)
            out.append(h[i:e])
            i = e
            continue
        start = m.start()
        depth, j = 0, start
        while True:
            o = h.find('<div', j)
            c = h.find('</div>', j)
            if c == -1:  # không còn thẻ đóng: giữ nguyên phần còn lại
                out.append(h[i:])
                return ''.join(out)
            if o != -1 and o < c:
                depth += 1
                j = o + 4
            else:
                depth -= 1
                j = c + 6
                if depth == 0:
                    break
        full = h[start:j]
        inner = h[m.end():j - 6]
        out.append(h[i:start])
        out.append(fn(full, inner))
        i = j
    return ''.join(out)


# Khối cấu trúc (không phải tin): prune/dedup không bao giờ được xóa —
# chính các khối này từng bị xóa oan mỗi lượt (FWMMX/VCSP "biến mất" bí ẩn).
_STRUCT_KEEP = ('VỐN', 'lta-ret-', 'NAV', 'CollegeAmerica',
                'ĐỌC TRONG 10 GIÂY', 'ĐỌC TRANG NÀY TRONG 10 GIÂY',
                'TÂM ĐIỂM', 'chưa phải tâm điểm')


def prune_old_alerts(h):
    """Tom lệnh 15/10: tin cũ TỰ BIẾN MẤT — chạy mỗi 5 phút, không chờ daily reset.
    - Xóa alert có ngày KHÔNG phải hôm nay (trừ Cú Đêm — xử riêng).
    - Cú Đêm: chỉ giữ 3 entry mới nhất.
    - Xóa alert không có ngày nào (tin rác, trừ mô tả ngắn)."""
    import datetime
    now = now_pt()
    today = now.strftime("%d/%m")
    removed = [0]

    def check_alert(full, inner):
        # Khối cấu trúc (ô phân bổ/tài khoản): không phải tin, không đụng
        if any(k in inner for k in _STRUCT_KEEP):
            return full
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

    h = sub_balanced_alerts(h, check_alert)

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
    def dedup_content(full, inner):
        # Khối cấu trúc có quyền lặp mã giữa các mục (vd VTV ở cả ltalloc lẫn
        # lt9): không dedup — xóa nguyên khối cân bằng thì không lệch div
        # nhưng vẫn mất nội dung.
        if any(k in inner for k in _STRUCT_KEEP):
            return full
        # Chuẩn hóa: lấy 80 ký tự đầu của text thuần
        txt = re.sub(r'<[^>]+>', '', inner).strip()[:80].lower()
        txt = re.sub(r'\s+', ' ', txt)
        if txt in seen_text:
            removed[0] += 1
            return ''
        seen_text.add(txt)
        return full
    h = sub_balanced_alerts(h, dedup_content)

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


def _zone_chip_text(t, px, st, z, bare=False):
    """(cls, text) của khung vùng: tên mã + trạng thái + giá + vùng + cắt.
    Dùng chung cho hàng bảng (tr) và khối FOCUS (div).
    bare=True (chỉ sec-special): chip chỉ ghi mã + trạng thái + giá —
    số VÙNG/CHỐT/CẮT duy nhất nằm ở khung vàng 🎫 LỆNH MUA trong cùng
    khối (luật Tom 2/11: khối ĐẶC BIỆT chỉ 1 bộ số, không trùng)."""
    lo, hi = z["entry"]
    note = (z.get("note") or "").upper()
    if "TAM VO HIEU" in note or "TEMP-INVALID" in note:
        return ("zchip-n", f"{t} · ⏸️ VÙNG TẠM TREO — CHỜ DỰNG NỀN · "
                           f"Giá {_lt_money(px)}")
    lab = badge_text(st)
    if st == "BROKEN":
        lab = "🔴 GÃY VÙNG — THỦNG CẮT"
    parts = [t, lab, f"Giá {_lt_money(px)}"]
    if not bare:
        parts.append(f"Vùng {_lt_money(lo)}–{_lt_money(hi)}")
        tg = z.get("targets") or []
        if len(tg) >= 2:
            parts.append(f"Chốt {_lt_money(tg[0])}→{_lt_money(tg[1])}")
        elif len(tg) == 1:
            parts.append(f"Chốt {_lt_money(tg[0])}")
        if z.get("cut"):
            parts.append(f"Cắt <{_lt_money(z['cut'])}")
        if st == "BELOW":
            parts.append(f"Còn +{_lt_pct1((lo / px - 1) * 100)} tới vùng")
    cls = {"IN": "zchip-g", "BROKEN": "zchip-r",
           "BELOW": "zchip-y", "ABOVE": "zchip-n"}[st]
    return cls, " · ".join(parts)


def _zone_chip_row(t, px, st, z, colspan=4):
    cls, txt = _zone_chip_text(t, px, st, z)
    return (f'<tr class="zchip-row" data-t="{t}"><td colspan="{colspan}">'
            f'<div class="zchip {cls}">{txt}</div></td></tr>')


def _zone_state_of(z, px):
    lo, hi = z["entry"]
    if z.get("cut") and px < z["cut"]:
        return "BROKEN"
    if lo <= px <= hi:
        return "IN"
    if px < lo:
        return "BELOW"
    return "ABOVE"


def _zones_local():
    """(zones, excluded) — ưu tiên ZONES global của pipeline, rớt về
    đọc zones.json cạnh script (test/tay)."""
    zones = ZONES
    excluded = set()
    try:
        raw = json.load(open(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "zones.json"), encoding="utf-8"))
        excluded = set(raw.get("_excluded", []))
        if not zones:
            zones = {t: v for t, v in raw.items()
                     if not t.startswith("_")}
    except Exception:
        pass
    return zones, excluded


def update_zone_chips(h, prices):
    """KHUNG CHỚP ĐẦU HÀNG cho bảng Sổ 1/2/3 (Tom chốt 30/10 08:51 — áp
    khuôn khung LONG TERM cho mọi mục): mỗi mã có vùng có 1 hàng khung
    ngay TRÊN dòng của nó — tên mã + trạng thái vùng + giá realtime +
    vùng + cắt. Chớp theo mức khẩn: IN xanh 1,9s · BROKEN đỏ 1,1s ·
    BELOW vàng 2,6s · ABOVE tĩnh không chớp. Chỉ chạy trong 3 section
    Sổ; mã excluded / không có giá / không có dòng thì bỏ qua."""
    zones, excluded = _zones_local()
    n = 0
    for sec, colspan in (("sec-longterm", 4), ("sec-so2", 4),
                         ("sec-so3", 4), ("sec-etf", 4),
                         ("sec-etf16", 4), ("sec-steelnet", 5),
                         ("sec-watch", 4)):
        m = re.search(r'<div class="sec" id="' + sec + r'"[^>]*>', h)
        if not m:
            continue
        start = m.start()
        nxt = h.find('<div class="sec"', start + 10)
        end = nxt if nxt != -1 else len(h)
        seg = h[start:end]
        for t in zones:
            if t in excluded or t not in prices:
                continue
            z = zones[t]
            if not z.get("entry"):
                continue
            row_pat = re.compile(r"<tr><td><b>" + re.escape(t)
                                 + r"</b></td>")
            if not row_pat.search(seg):
                continue
            px = prices[t][0]
            chip = _zone_chip_row(t, px, _zone_state_of(z, px), z,
                                  colspan=colspan)
            cpat = re.compile(r'<tr class="zchip-row" data-t="'
                              + re.escape(t) + r'">.*?</tr>', re.S)
            seg, cn = cpat.subn(chip, seg, count=1)
            if not cn:
                seg = row_pat.sub(lambda mm: chip + mm.group(0),
                                  seg, count=1)
            n += 1
        # Hàng KHÔNG có vùng (watch-only / chờ dựng nền): vẫn có khung
        # tĩnh trên đầu cho đồng nhất toàn bảng (Tom chốt 30/10 09:05 —
        # TẤT CẢ các mục): 👁️ THEO DÕI / ⏸️ CHỜ DỰNG NỀN, không chớp.
        for t in re.findall(r"<tr><td><b>([A-Z]{2,6})</b></td>", seg):
            if t in excluded or f'data-t="{t}"' in seg:
                continue
            if t in prices:
                px = prices[t][0]
            else:
                pm = re.search(r"<tr><td><b>" + re.escape(t)
                               + r"</b></td><td>(?:NAV )?\$([\d,]+\.?\d*)",
                               seg)
                if not pm:
                    continue
                px = float(pm.group(1).replace(",", ""))
            if t in zones or t == "NFLX":
                txt = f"{t} · ⏸️ CHỜ DỰNG NỀN · Giá {_lt_money(px)}"
            else:
                txt = f"{t} · 👁️ THEO DÕI · Giá {_lt_money(px)}"
            chip = (f'<tr class="zchip-row" data-t="{t}">'
                    f'<td colspan="{colspan}">'
                    f'<div class="zchip zchip-n">{txt}</div></td></tr>')
            row_pat = re.compile(r"<tr><td><b>" + re.escape(t)
                                 + r"</b></td>")
            seg = row_pat.sub(lambda mm: chip + mm.group(0), seg, count=1)
            n += 1
        h = h[:start] + seg + h[end:]
    return h, n


FOCUS_BLOCK_PAT = re.compile(
    r'(<div style="border-top:1px solid rgba\(212,175,55,\.45\);'
    r'padding:7px 0;word-break:break-word">)'
    r'(<div class="zchip[^"]*" data-fchip="[A-Z]{2,6}">.*?</div>)?'
    r'(🦈+ <b>([A-Z]{2,6})</b> \$([\d,]+\.?\d*))', re.S)


def update_focus_chips(h, prices):
    """KHUNG CHỚP ĐẦU KHỐI cho 👑 FOCUS (chặng 2, Tom 30/10): mỗi khối
    cá mập được 1 khung zchip ngay trên dòng 🦈🦈🦈 — cùng khuôn Sổ:
    tên mã + trạng thái vùng + giá + vùng + cắt, chớp theo mức khẩn.
    Giá lấy từ prices, rớt về giá in sẵn trong khối."""
    zones, excluded = _zones_local()
    n = 0

    def rep(m):
        nonlocal n
        t = m.group(4)
        if t in excluded or t not in zones or not zones[t].get("entry"):
            return m.group(0)
        if t in prices:
            px = prices[t][0]
        else:
            px = float(m.group(5).replace(",", ""))
        cls, txt = _zone_chip_text(t, px, _zone_state_of(zones[t], px),
                                   zones[t])
        n += 1
        return (m.group(1)
                + f'<div class="zchip {cls}" data-fchip="{t}">{txt}</div>'
                + m.group(3))

    h = FOCUS_BLOCK_PAT.sub(rep, h)
    return h, n


ALERT_CHIP_PAT = re.compile(
    r'(<div class=["\']alert[^"\']*["\'][^>]*>)'
    r'(?:(<div class="zchip[^"]*" data-achip="([A-Z]{2,6})">.*?</div>))?'
    r'(\s*<b>(.{0,120}?)</b>)?'
    r'(.{0,60})', re.S)

# Mục KHÔNG bỏ tiêu đề alert khi gắn khung: khối kim cương ĐẶC BIỆT —
# tiêu đề mang thứ hạng TÂM ĐIỂM SỐ 1/2/3, là nội dung chứ không trùng.
NO_STRIP_SECS = {"sec-special"}

ALERT_CHIP_SECS = ("sec-special", "sec-topfocus", "sec-news",
                   "sec-ahshark", "sec-asia", "sec-urgent",
                   "sec-tinhhoa", "sec-chinam", "sec-hot", "sec-live",
                   "sec-plan", "sec-daytrade", "sec-cudem", "sec-watch",
                   "sec-swing", "sec-dram", "sec-lt9")


def update_alert_chips(h, prices):
    """KHUNG ĐẦU KHỐI cho MỌI mục dạng alert (Tom chốt 30/10 09:01 — cả
    daytrade / swing / long-t9 / tin / earnings cũng như vậy): alert nào
    mở đầu bằng <b> chứa mã có vùng thì gắn 1 khung zchip ngay đầu khối —
    cùng khuôn: tên mã + trạng thái vùng + giá realtime + vùng + cắt,
    chớp theo mức khẩn. Alert không dẫn bằng mã có vùng thì giữ nguyên.
    Giá: prices trước, rớt về giá in sẵn sau tiêu đề alert."""
    zones, excluded = _zones_local()
    n = 0
    for sec in ALERT_CHIP_SECS:
        m = re.search(r'<div class="sec" id="' + sec + r'"[^>]*>', h)
        if not m:
            continue
        start = m.start()
        nxt = h.find('<div class="sec"', start + 10)
        end = nxt if nxt != -1 else len(h)
        seg = h[start:end]

        def rep(mm):
            nonlocal n
            # Mã: khung cũ (data-achip) trước — alert đã bỏ tiêu đề ở lượt
            # trước vẫn làm mới được khung; chưa có khung thì đọc tiêu đề.
            t = mm.group(3)
            if t is None and mm.group(5):
                for tok in re.findall(r"[A-Z]{2,6}", mm.group(5)):
                    if tok in zones and tok not in excluded \
                            and zones[tok].get("entry"):
                        t = tok
                        break
            if t is None or t in excluded \
                    or not zones.get(t, {}).get("entry"):
                return mm.group(0)
            if t in prices:
                px = prices[t][0]
            else:
                pm = re.search(r"\$([\d,]+\.?\d*)", mm.group(6) or "")
                if not pm and mm.group(2):
                    pm = re.search(r"Giá \$([\d,]+\.?\d*)", mm.group(2))
                if not pm:
                    return mm.group(0)
                px = float(pm.group(1).replace(",", ""))
            cls, txt = _zone_chip_text(t, px,
                                       _zone_state_of(zones[t], px),
                                       zones[t],
                                       bare=(sec == "sec-special"))
            n += 1
            tail = mm.group(6) or ""
            if mm.group(4):
                # Tin Yahoo: tiêu đề là nội dung tin, KHÔNG được bỏ dù
                # mã có vùng (lỗi 02/11: tiêu đề AMD bị chip nuốt mất).
                nxt = seg.find('<div class="alert"', mm.end())
                blk = seg[mm.start(): nxt if nxt != -1 else len(seg)]
                if "Yahoo Finance qua" in blk:
                    return (mm.group(1)
                            + f'<div class="zchip {cls}" data-achip="{t}">'
                            + txt + "</div>"
                            + mm.group(4) + tail)
            if mm.group(4) and sec not in NO_STRIP_SECS:
                # KHUNG THAY TIÊU ĐỀ (Tom bắt lỗi trùng 30/10): bỏ headline
                # <b>...</b> vì khung đã ghi mã + trạng thái + giá + vùng +
                # cắt; dọn dấu nối + đoạn giá/giờ lặp ở đầu phần còn lại.
                tail = re.sub(r"^\s*(—|–|-)\s*", "", tail)
                tail = re.sub(
                    r"^\$[\d,]+\.?\d*\s*\([−+\-][\d.,]+%\)\s*"
                    r"lúc\s*[\d:]{4,5}\s*PT\.?\s*", "", tail)
                tail = re.sub(r"^(<br\s*/?>)+", "", tail)
                tail = re.sub(r"^\s*(—|–|-)\s*", "", tail)
                return (mm.group(1)
                        + f'<div class="zchip {cls}" data-achip="{t}">'
                        + txt + "</div>" + tail)
            return (mm.group(1)
                    + f'<div class="zchip {cls}" data-achip="{t}">{txt}</div>'
                    + (mm.group(4) or "") + tail)

        seg = ALERT_CHIP_PAT.sub(rep, seg)
        h = h[:start] + seg + h[end:]
    return h, n


# =====================================================================
# ĐỒNG BỘ SỐ TĨNH + LÀM GIÀU DAYTRADE — Tom bắt lỗi 04/11/2026 05:36 PT:
# daytrade/swing/long/leverage mỗi mục một số khác nhau vì mỗi writer
# đóng băng giá/%/trạng thái/vùng tại thời điểm NÓ chạy; chỉ chip
# data-px mới sống nhờ poller. Từ nay MỌI lượt refresh (lite premarket
# + full) chạy pass đồng bộ CUỐI pipeline: mọi số IN trên bảng trong
# các mục đích tính lại từ CÙNG 1 bộ giá (ngoài giờ = fetch_pm_prices,
# trong phiên = giá intraday) + zones.json. Một công thức dịch vùng xe
# duy nhất (_comp_line / _lev_pair_block đều dùng r = giá xe / giá gốc
# trong CÙNG dict giá của lượt) — cấm trộn giá premarket với giá đóng
# cửa trong cùng 1 lượt (gốc vụ AMDL 2 nơi 2 vùng dịch).
# =====================================================================
SYNC_SECS = ("sec-daytrade", "sec-swing", "sec-longterm", "sec-so2",
             "sec-so3", "sec-leverage", "sec-special", "sec-topfocus",
             "sec-plan", "sec-hot", "sec-watch", "sec-steelnet",
             "sec-etf", "sec-etf16", "sec-dram", "sec-ahshark")
_STATUS_LAB = {"IN": "🟢 TRONG VÙNG", "ABOVE": "⚪ TRÊN VÙNG",
               "BELOW": "🟡 DƯỚI VÙNG",
               "BROKEN": "🔴 GÃY VÙNG — THỦNG CẮT"}
_ZONE_PHRASES = [
    "🔴 GÃY VÙNG — THỦNG CẮT", "Vượt lên trên vùng", "vượt lên trên vùng",
    "Vượt khỏi vùng", "vượt khỏi vùng", "Sát mép trên vùng",
    "Sát mép dưới vùng", "sát mép trên vùng", "sát mép dưới vùng",
    "Ngay mép dưới vùng", "Ngay mép trên vùng", "🟢 TRONG VÙNG",
    "🟡 DƯỚI VÙNG", "⚪ TRÊN VÙNG", "🔴 GÃY VÙNG", "TRONG VÙNG",
    "DƯỚI VÙNG", "TRÊN VÙNG", "GÃY VÙNG", "TRONG vùng", "DƯỚI vùng",
    "TRÊN vùng", "Trên vùng", "Dưới vùng", "Trong vùng"]


def _parse_num_sync(s):
    """'1.234,56' VN / '1,234.56' US / '483,9' VN / '855.10' US -> float."""
    s = s.strip().replace(" ", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s):
        return float(s.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):
        return float(s.replace(",", ""))
    if re.fullmatch(r"\d+,\d{1,2}", s):
        return float(s.replace(",", "."))
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    return None


def _fmt_like(old, v):
    """Format tiền khớp khuôn chuỗi cũ: thập phân phẩy kiểu VN ($511,29)
    -> VN; còn lại US 2 số lẻ ($855.10)."""
    if re.search(r",\d{1,2}$", old):
        return fmt_price_vn(v)[1:]
    return f"{v:,.2f}"


def _zone_vals(z):
    vals = [float(x) for x in (z.get("entry") or [])]
    vals += [float(x) for x in (z.get("targets") or [])]
    if z.get("cut"):
        vals.append(float(z["cut"]))
    return vals


def _alert_spans(seg):
    """(start, end) của từng khối <div class="alert..."> bằng stack-walk
    div (bài học cân bằng div 19/10 — không regex non-greedy)."""
    out = []
    for m in re.finditer(r'<div class="alert[^"]*">', seg):
        depth, end = 0, None
        for dm in re.finditer(r"</?div\b[^>]*>", seg[m.start():]):
            depth += -1 if dm.group(0).startswith("</") else 1
            if depth == 0:
                end = m.start() + dm.end()
                break
        if end:
            out.append((m.start(), end))
    return out


def _mask_blocks(seg):
    """Che các khối con đã có updater riêng (zchip/levcomp/levmap +
    span data-px/data-pct) để pass văn xuôi không đụng vào; trả
    (bản che, kho khôi phục)."""
    store = []

    def _put(txt):
        store.append(txt)
        return f"\x01{len(store) - 1}\x02"

    # xử lý từ cuối về đầu để offset không lệch
    spans = []
    for m in re.finditer(r'<div class="(?:zchip|levcomp|levmap)[^"]*">',
                         seg):
        depth, end = 0, None
        for dm in re.finditer(r"</?div\b[^>]*>", seg[m.start():]):
            depth += -1 if dm.group(0).startswith("</") else 1
            if depth == 0:
                end = m.start() + dm.end()
                break
        if end:
            spans.append((m.start(), end))
    for s0, s1 in reversed(spans):
        seg = seg[:s0] + _put(seg[s0:s1]) + seg[s1:]
    seg = re.sub(r'<span[^>]*data-(?:px|pct)="[^"]*"[^>]*>.*?</span>',
                 lambda m: _put(m.group(0)), seg, flags=re.S)
    return seg, store


def _unmask(seg, store):
    for i, txt in enumerate(store):
        seg = seg.replace(f"\x01{i}\x02", txt)
    return seg


def _prose_price_sub(txt, prices, zones):
    """Giá trong VĂN XUÔI (alert/kế hoạch): 'AMD premarket $511,29
    (+5,66%, 03:39 PT)' -> giá + % + giờ HIỆN TẠI. Pattern chỉ khớp mã
    CÓ trong dict giá (tránh mã giả 'PT' trong '03:39 PT' nuốt mất
    match của mã thật đứng sau). Bỏ qua: số vùng/chốt/cắt của chính mã
    (chỉ khi KHÔNG kèm '(+x%)' — số kèm % luôn là giá), số gắn nhãn
    'đóng cửa'/'Giá hiện tại', mốc kệ/đáy/MA trong ghi chú, đầu một
    khoảng giá, số doanh thu $X tỷ, số lệch >35% (mốc lịch sử)."""
    ticks = sorted((t for t in prices if len(t) >= 2),
                   key=len, reverse=True)
    if not ticks:
        return txt
    _alt = "|".join(map(re.escape, ticks))
    # mid không được chứa mã khác có giá (tránh gán nhầm giá của mã
    # đứng sau cho mã đứng trước trong câu nhiều mã — vụ RTX/RCL).
    pat = re.compile(
        r"\b(" + _alt + r")\b"
        r"((?:(?![$%]|\b(?:" + _alt + r")\b).){0,30}?)\$\s*([\d][\d.,]*)"
        r"(\s*\([−+\-]?\s*[\d.,]+%(?:\s*,\s*(\d{1,2}:\d{2})\s*PT)?\))?"
        r"(?!\s*tỷ)")

    def rep(m):
        t, mid, num, pctpart, _tm = m.groups()
        low = mid.lower()
        if "hiện tại" in low or "đóng" in low:
            return m.group(0)
        if any(w in low for w in ("kệ", "đáy", "đỉnh", "hỗ trợ",
                                  "kháng cự", "ma20", "ma50", "ma200",
                                  "đồng thuận", "52 tuần", "52w")):
            return m.group(0)
        if re.match(r"\s*[–\-]\s*\$?[\d.,]+", txt[m.end():m.end() + 14]):
            return m.group(0)  # đầu một KHOẢNG (kệ/vùng) — là MỐC
        val = _parse_num_sync(num)
        if val is None:
            return m.group(0)
        z = zones.get(t)
        if z and not pctpart:
            for zv in _zone_vals(z):
                if abs(val - zv) <= max(0.51, abs(zv) * 0.003):
                    return m.group(0)
            tail = re.sub(r"[\s·•|]+$", "", low)[-16:]
            if re.search(r"(vùng|vào|chốt|cắt|target|trong|trên|dưới|"
                         r"mép|sát|tới)\s*<?$", tail):
                return m.group(0)
        px, pc = prices[t]
        if not px or abs(val - px) / px > 0.35:
            return m.group(0)
        out = f"{t}{mid}${_fmt_like(num, px)}"
        if pctpart:
            if _tm:
                out += (f" ({fmt_pct_vn(pc)}, "
                        f"{now_pt().strftime('%H:%M')} PT)")
            else:
                out += f" ({fmt_pct_vn(pc)})"
        return out
    return pat.sub(rep, txt)


def _sync_data_px(h, prices):
    """Mọi <span data-px="T">T $X</span> + <span data-pct="T">(Y%)</span>
    trên TOÀN bảng -> giá/% hiện tại (khuôn _lt_money như lúc render).
    Trước đây bản HTML thô kẹt giá lúc render — khối cặp đòn bẩy đóng
    băng giá ĐÓNG CỬA suốt premarket vì lite không render lại."""
    n = 0

    def _px(m):
        nonlocal n
        t = m.group(2)
        if t not in prices:
            return m.group(0)
        n += 1
        return f"{m.group(1)}{t} {_lt_money(prices[t][0])}{m.group(3)}"

    h = re.sub(r'(<span[^>]*data-px="([A-Z]{1,6})"[^>]*>)[A-Z]{1,6} '
               r"\$[\d.,]+(</span>)", _px, h)

    def _pc(m):
        nonlocal n
        t = m.group(2)
        if t not in prices:
            return m.group(0)
        n += 1
        return f"{m.group(1)}({fmt_pct_vn(prices[t][1])}){m.group(3)}"

    h = re.sub(r'(<span[^>]*data-pct="([A-Z]{1,6})"[^>]*>)\([−+\-]?'
               r"[\d.,]+%\)(</span>)", _pc, h)
    return h, n


def _sync_zchips(seg, prices, zones, bare=False):
    """Dựng lại MỌI zchip (data-achip / zchip-row / data-lchip) trong 1
    section bằng _zone_chip_text — 1 khuôn duy nhất: giá + trạng thái +
    vùng + chốt + cắt đều từ prices/zones.json HIỆN TẠI. Kể cả mã thuộc
    _excluded (vd SKHY ở daytrade theo lệnh Tom 04/11): các updater cũ
    bỏ qua excluded nên chip của chúng đóng băng."""
    n = 0

    def _mk(t):
        z = zones.get(t)
        if not z or not z.get("entry") or t not in prices:
            return None
        return _zone_chip_text(t, prices[t][0],
                               _zone_state_of(z, prices[t][0]), z,
                               bare=bare)

    def _achip(m):
        nonlocal n
        r = _mk(m.group(1))
        if not r:
            return m.group(0)
        n += 1
        return (f'<div class="zchip {r[0]}" data-achip="{m.group(1)}">'
                f"{r[1]}</div>")

    seg = re.sub(r'<div class="zchip[^"]*" data-achip="([A-Z]{2,6})">'
                 r".*?</div>", _achip, seg, flags=re.S)

    def _row(m):
        nonlocal n
        r = _mk(m.group(1))
        if not r:
            return m.group(0)
        n += 1
        return (f'<tr class="zchip-row" data-t="{m.group(1)}">'
                f'<td colspan="{m.group(2)}">'
                f'<div class="zchip {r[0]}">{r[1]}</div></td></tr>')

    seg = re.sub(r'<tr class="zchip-row" data-t="([A-Z]{2,6})">'
                 r'<td colspan="(\d+)"><div class="zchip[^"]*">.*?</div>'
                 r"</td></tr>", _row, seg, flags=re.S)

    def _lchip(m):
        nonlocal n
        v = m.group(1)
        u = LEV_MAP.get(v)
        if not u or v not in prices or u not in prices:
            return m.group(0)
        vpx, vpct = prices[v]
        head = f"{v} {_lt_money(vpx)} ({fmt_pct_vn(vpct)}) · NEO "
        z = zones.get(u)
        if z and z.get("entry"):
            cls, txt = _zone_chip_text(
                u, prices[u][0], _zone_state_of(z, prices[u][0]), z)
        else:
            cls = "zchip-n"
            txt = (f"{u} · ⚪ CHƯA CÓ VÙNG · "
                   f"Giá gốc {_lt_money(prices[u][0])}")
        n += 1
        return (f'<div class="zchip {cls}" data-lchip="{v}">'
                f"{head}{txt}</div>")

    seg = re.sub(r'<div class="zchip[^"]*" data-lchip="([A-Z]{1,6})">'
                 r".*?</div>", _lchip, seg, flags=re.S)

    # Chip tĩnh 👁️ THEO DÕI / ⏸️ CHỜ DỰNG NỀN (mã không có vùng):
    # update_zone_chips chỉ TẠO khi thiếu, không bao giờ cập nhật lại
    # giá trong chip đã có -> chip kẹt giá cũ (vụ VFLO $53,57 trong
    # khi giá dòng đã $54,40).
    def _watch(m):
        nonlocal n
        t = m.group(2)
        if t not in prices or _mk(t):
            return m.group(0)
        n += 1
        return (f"{m.group(1)}{t} · {m.group(3)} · "
                f"Giá {_lt_money(prices[t][0])}{m.group(4)}")

    seg = re.sub(r'(<div class="zchip[^"]*">)([A-Z]{2,6}) · '
                 r"((?:👁️ THEO DÕI|⏸️ CHỜ DỰNG NỀN)) · Giá "
                 r"\$[\d.,]+(</div>)", _watch, seg)
    return seg, n


_ROW_ZONE_PAT = re.compile(
    r"Vào\s*\$[\d.,]+[–\-]\$?[\d.,]+\s*•\s*Chốt\s*\$[\d.,]+"
    r"(?:→\$?[\d.,]+)?\s*•\s*(?:<span class=\"cut\">\s*)?"
    r"Cắt(?:\s*dưới)?\s*<?\$[\d.,]+(?:\s*</span>)?")


def _us_money(v):
    f = float(v)
    return f"${f:,.0f}" if f == int(f) else f"${f:,.2f}"


def _sync_rows(seg, prices, zones):
    """Hàng bảng <tr><td><b>T</b></td>...: (1) khuôn vùng trong
    span.zone viết lại từ zones.json (bắt vụ MU swing còn vùng cũ
    $890–920/cắt $870 sau khi zones.json đã vẽ lại $840–870);
    (2) badge trạng thái vùng của hàng tính lại từ giá hiện tại —
    lite premarket trước đây KHÔNG chạy update_zone_badges nên badge
    kẹt từ lượt full hôm trước."""
    n = 0

    def _row(m):
        nonlocal n
        t, body = m.group(1), m.group(2)
        z = zones.get(t)
        if not z or not z.get("entry"):
            return m.group(0)
        changed = False

        def _zone(_mz):
            lo, hi = z["entry"]
            tg = z.get("targets") or []
            s = f"Vào {_us_money(lo)}–{_us_money(hi)} • Chốt "
            s += "→".join(_us_money(x) for x in tg[:2]) if tg else "—"
            if z.get("cut"):
                s += f" • Cắt dưới {_us_money(z['cut'])}"
            return s

        body2, cn = _ROW_ZONE_PAT.subn(_zone, body, count=1)
        if cn and body2 != body:
            body, changed = body2, True
        if t in prices:
            st = _zone_state_of(z, prices[t][0])

            def _badge(mb):
                cur = re.sub(r"<[^>]+>", "", mb.group(4))
                if not any(w in cur for w in ("VÙNG", "CẮT")):
                    return mb.group(0)
                return (f"{mb.group(1)}{badge_class(st)}{mb.group(3)}"
                        f"{badge_text(st)}{mb.group(5)}")

            body2, cn = re.subn(
                r'(<span class="badge )(b-[a-z]+)(">)(.*?)(</span>)',
                _badge, body, count=1, flags=re.S)
            if cn and body2 != body:
                body, changed = body2, True
        if changed:
            n += 1
            return f"<tr><td><b>{t}</b></td>" + body + "</tr>"
        return m.group(0)

    seg = re.sub(r"<tr><td><b>([A-Z]{2,6})</b></td>(.*?)</tr>", _row,
                 seg, flags=re.S)
    return seg, n


def _sync_alerts(seg, prices, zones):
    """Trong từng alert: giá văn xuôi -> giá hiện tại; CỤM TỪ trạng
    thái vùng trong văn xuôi (vd 'Vượt lên trên vùng' viết lúc giá
    $511) -> nhãn trạng thái tính từ giá HIỆN TẠI của mã chính của
    alert (bắt vụ AMD: chip ghi GÃY mà văn xuôi ghi VƯỢT VÙNG trong
    cùng 1 khối). Chỉ thay cụm từ trên dòng không nhắc mã vùng khác."""
    spans = _alert_spans(seg)
    for s0, s1 in reversed(spans):
        blk = seg[s0:s1]
        t = None
        m = re.search(r'data-achip="([A-Z]{2,6})"', blk)
        if m:
            t = m.group(1)
        if not t:
            t = _comp_alert_ticker(blk)
        masked, store = _mask_blocks(blk)
        masked = _prose_price_sub(masked, prices, zones)
        if t and t in prices and zones.get(t, {}).get("entry"):
            lab = _STATUS_LAB[_zone_state_of(zones[t], prices[t][0])]
            # thay cụm từ trong MỘT lượt regex (dài trước) — thay tuần
            # tự từng cụm sẽ tự thay cả vào nhãn vừa chèn (vụ nhãn
            # 'GÃY VÙNG' bị nhân ba 04/11).
            phrase_re = re.compile("|".join(
                re.escape(p) for p in
                sorted(_ZONE_PHRASES, key=len, reverse=True)))
            lines = masked.split("<br>")
            for i, ln in enumerate(lines):
                toks = set(re.findall(r"\b([A-Z]{2,6})\b",
                                      re.sub(r"<[^>]+>", " ", ln)))
                if any(x in zones and x != t for x in toks):
                    continue
                lines[i] = phrase_re.sub(lab, ln)
            masked = "<br>".join(lines)
        seg = seg[:s0] + _unmask(masked, store) + seg[s1:]
    return seg


def _sync_plan(seg, prices, zones):
    """Khối '📍 VÙNG ĐANG MỞ' trong sec-plan: dựng lại 3 dòng trạng
    thái (TRONG VÙNG / VƯỢT VÙNG / SÁT-DƯỚI VÙNG) từ giá HIỆN TẠI cho
    đúng các mã khối đang nêu (bản 04:00 đóng băng giá premarket mỏng
    03:39 PT — AMD $511,29 'vượt vùng' trong khi giá thật đã về $486).
    Khối earnings cùng tiêu đề (không có dòng TRONG VÙNG) giữ nguyên."""
    n = 0
    pat = re.compile(r'<div class="alert"><b>📍 VÙNG ĐANG MỞ — SÁNG '
                     r"(\d{2}/\d{2}) \(giá [^)]*\)</b><br>(.*?)</div>",
                     re.S)
    for m in reversed(list(pat.finditer(seg))):
        body = m.group(2)
        if "TRONG VÙNG" not in body:
            continue
        ticks = []
        for tm in re.finditer(r"\b([A-Z]{2,6})\s*\$[\d.,]+", body):
            t = tm.group(1)
            if t in zones and zones[t].get("entry") and t in prices \
                    and t not in ticks:
                ticks.append(t)
        if not ticks:
            continue
        g_in, g_ab, g_near = [], [], []
        for t in ticks:
            z = zones[t]
            px = prices[t][0]
            lo, hi = z["entry"]
            st = _zone_state_of(z, px)
            if st == "IN":
                g_in.append(f"{t} {_lt_money(px)} trong "
                            f"{_lt_money(lo)}–{_lt_money(hi)}")
            elif st == "ABOVE":
                g_ab.append(f"{t} {_lt_money(px)} trên "
                            f"{_lt_money(lo)}–{_lt_money(hi)}")
            elif st == "BROKEN":
                g_near.append(f"{t} {_lt_money(px)} dưới cắt "
                              f"{_lt_money(z['cut'])}")
            else:
                g_near.append(f"{t} {_lt_money(px)} dưới vùng "
                              f"{_lt_money(lo)}–{_lt_money(hi)}, còn "
                              f"trên cắt {_lt_money(z['cut'])}"
                              if z.get("cut") else
                              f"{t} {_lt_money(px)} dưới vùng "
                              f"{_lt_money(lo)}–{_lt_money(hi)}")
        lines = []
        if g_in:
            lines.append("· TRONG VÙNG: " + " · ".join(g_in))
        if g_ab:
            lines.append("· VƯỢT VÙNG LÊN TRÊN: " + " · ".join(g_ab))
        if g_near:
            lines.append("· SÁT VÙNG / DƯỚI VÙNG: " + " · ".join(g_near))
        new = (f'<div class="alert"><b>📍 VÙNG ĐANG MỞ — SÁNG '
               f"{m.group(1)} (giá cập nhật "
               f"{now_pt().strftime('%H:%M')} PT)</b><br>"
               + "<br>\n".join(lines) + "</div>")
        seg = seg[:m.start()] + new + seg[m.end():]
        n += 1
    return seg, n


def _sync_tinhhoa(seg, prices):
    """Entry earnings: 'Giá hiện tại $X (+Y% hôm nay) · cập nhật
    HH:MM PT' -> số + giờ hiện tại (mã lấy từ <b> đầu entry)."""
    n = 0
    pat = re.compile(r"(Giá hiện tại )\$([\d.,]+)(\s*\()([−+\-]?"
                     r"[\d.,]+%)(\) hôm nay · cập nhật )"
                     r"(\d{1,2}:\d{2} PT)")
    for s0, s1 in reversed(_alert_spans(seg)):
        blk = seg[s0:s1]
        if "Giá hiện tại" not in blk:
            continue
        bm = re.search(r"<b>([A-Z]{2,6})</b>", blk)
        if not bm or bm.group(1) not in prices:
            continue
        px, pc = prices[bm.group(1)]

        def _rep(m):
            return (f"{m.group(1)}${_fmt_like(m.group(2), px)}"
                    f"{m.group(3)}{pc:+.1f}%{m.group(5)}"
                    f"{now_pt().strftime('%H:%M')} PT")

        blk2, cn = pat.subn(_rep, blk)
        if cn:
            seg = seg[:s0] + blk2 + seg[s1:]
            n += cn
    return seg, n


def sync_static_numbers(h, prices):
    """PASS ĐỒNG BỘ SỐ TĨNH — chạy CUỐI mọi lượt refresh (lite + full).
    Trả (h, tổng chỗ sửa). Fail-soft từng section: lỗi 1 mục không
    được làm chết cả lượt."""
    zones, _excl = _zones_local()
    total = 0
    # bổ sung giá cho mã chỉ xuất hiện trong các mục đích (vd ANET ở
    # topfocus — không có vùng, không có dòng bảng nên prices của
    # pipeline chính không chứa) bằng fetch_pm_prices (prepost-aware:
    # đúng cả ngoài giờ lẫn trong phiên); thiếu nữa thì giữ nguyên số.
    try:
        cand = set()
        for sid in SYNC_SECS:
            b = _sec_bounds(h, sid)
            if b:
                cand |= set(re.findall(r"\b([A-Z]{2,6})\b",
                                       re.sub(r"<[^>]+>", " ",
                                              h[b[0]:b[1]])))
        db = _dayplan_bounds(h)
        if db:
            cand |= set(re.findall(r"\b([A-Z]{2,6})\b",
                                   re.sub(r"<[^>]+>", " ",
                                          h[db[0]:db[1]])))
        miss = [t for t in sorted(cand) if t not in prices]
        if miss:
            got = fetch_pm_prices(miss)
            prices = {**prices, **got}
    except Exception as e:
        print(f"  ! sync prices bổ sung: {e}", file=sys.stderr)
    h, nd = _sync_data_px(h, prices)
    total += nd
    for sid in SYNC_SECS:
        try:
            # Khoảng section = tới section kế tiếp (khuôn update_zone_
            # chips) — stack-walk có thể kết thúc SỚM ở section vỡ lồng
            # nhau (sec-ahshark: div đầu không đóng, 4 alert nằm ngoài
            # bounds) làm pass không với tới. Section cuối dùng
            # stack-walk để tránh nuốt footer/script.
            ms = re.search(r'<div class="sec" id="' + sid + r'"[^>]*>', h)
            if not ms:
                continue
            s0 = ms.start()
            nxt = h.find('<div class="sec"', s0 + 10)
            if nxt != -1:
                b = (s0, nxt)
            else:
                b = _sec_bounds(h, sid)
                if not b:
                    continue
            seg = h[b[0]:b[1]]
            seg, n1 = _sync_zchips(seg, prices, zones,
                                   bare=(sid == "sec-special"))
            seg, n2 = _sync_rows(seg, prices, zones)
            seg = _sync_alerts(seg, prices, zones)
            if sid == "sec-plan":
                seg, n3 = _sync_plan(seg, prices, zones)
                total += n3
            total += n1 + n2
            h = h[:b[0]] + seg + h[b[1]:]
        except Exception as e:
            print(f"  ! sync {sid}: {e}", file=sys.stderr)
    try:
        db = _dayplan_bounds(h)
        if db:
            seg = h[db[0]:db[1]]
            seg, n1 = _sync_zchips(seg, prices, zones)
            seg = _sync_alerts(seg, prices, zones)
            total += n1
            h = h[:db[0]] + seg + h[db[1]:]
    except Exception as e:
        print(f"  ! sync dayPlan: {e}", file=sys.stderr)
    try:
        b = _sec_bounds(h, "sec-tinhhoa")
        if b:
            seg, n4 = _sync_tinhhoa(h[b[0]:b[1]], prices)
            total += n4
            h = h[:b[0]] + seg + h[b[1]:]
    except Exception as e:
        print(f"  ! sync tinhhoa: {e}", file=sys.stderr)
    return h, total


# --- LÀM GIÀU DAYTRADE (Tom lệnh 04/11 05:42 PT: mục daytrade ~9 mã
# là "nghèo, xấu hổ") — khối DAY-HOT-AUTO dựng lại MỖI LƯỢT từ giá +
# zones.json hiện tại: ~18–22 khối chia 3 nhóm BAY / ÉP VÙNG / ĐẬP,
# mỗi khối đủ chip vùng (hoặc watch-only rõ ràng) + dòng xe do
# update_companion_lines gắn ngay sau (cùng 1 công thức toàn bảng) +
# tem nhị phân cho mã có earnings hôm nay/ngày mai. Alert tay không
# có chip (vd ⏰ XÚC TÁC) được GIỮ NGUYÊN; ghi chú tay của khối cũ
# được giữ lại nếu không chứa trạng thái vùng/số giá cũ.
DAY_HOT_POOL = ["ANET", "AMZN", "BABA", "MSFT", "CRM", "MELI", "NOW",
                "UAL", "NKE", "GLD", "SLV", "TNA", "SNDK", "SKHY",
                "MU", "NVDA", "AVGO", "COHR", "TSLA", "PLTR", "ALAB",
                "IREN", "FRSH", "HOOD", "QCOM", "AMD", "GOOGL", "SMCI",
                "TSM", "COIN", "MSTR", "APP", "ARM"]
DAY_CORE_KEEP = {"AMD", "ANET", "AMZN", "GOOGL", "AVGO", "NVDA",
                 "COHR", "QCOM"}
DAY_BINARY = {"MELI": (11, 4), "COHR": (11, 4), "QCOM": (11, 4),
              "IREN": (11, 5), "AXON": (11, 5), "APP": (11, 4),
              "ARM": (11, 4)}  # (tháng, ngày) earnings — tem nhị phân


def _day_notes(seg, zones):
    """Ghi chú tay theo mã từ các alert chip cũ của sec-daytrade: giữ
    dòng KHÔNG chứa chữ 'vùng' và KHÔNG chứa số giá (trừ $X tỷ) — các
    dòng trạng thái/số cũ bị loại để khối mới không tự mâu thuẫn."""
    notes = {}
    for s0, s1 in _alert_spans(seg):
        blk = seg[s0:s1]
        m = re.search(r'data-achip="([A-Z]{2,6})"', blk)
        if not m:
            continue
        t = m.group(1)
        body = re.sub(r'^\s*<div class="alert[^"]*">', "", blk)
        body = re.sub(r"</div>\s*$", "", body)
        body = re.sub(r'<div class="(?:zchip|levcomp)[^"]*">.*?</div>',
                      "", body, flags=re.S)
        keep = []
        for ln in body.split("<br>"):
            txt = re.sub(r"<[^>]+>", " ", ln)
            txt = re.sub(r"\s+", " ", txt).strip()
            if len(txt) < 12 or "vùng" in txt.lower():
                continue
            if re.search(r"\$[\d.,]+(?!\s*tỷ)", txt):
                continue
            keep.append(ln.strip())
            if len(keep) >= 2:
                break
        if keep:
            notes[t] = keep
    return notes


def _day_block(t, prices, zones, notes, binary):
    px, pc = prices[t]
    z = zones.get(t)
    parts = []
    if z and z.get("entry"):
        cls, txt = _zone_chip_text(t, px, _zone_state_of(z, px), z)
        parts.append(f'<div class="alert"><div class="zchip {cls}" '
                     f'data-achip="{t}">{txt}</div>')
        if _zone_state_of(z, px) == "BROKEN":
            parts.append("· Giá dưới cắt — vùng chỉ còn tham chiếu, "
                         "chờ nến ĐÓNG lấy lại cắt mới tính lại")
    else:
        parts.append(
            f'<div class="alert"><b><span data-px="{t}">'
            f"{t} {_lt_money(px)}</span></b> "
            f'<span data-pct="{t}">({fmt_pct_vn(pc)})</span>'
            f" · 👁️ WATCH-ONLY")
        parts.append("· ⚪ Chưa có vùng duyệt — chỉ theo dõi, "
                     "không lên mâm")
    if binary:
        parts.append(f"· 💥 NHỊ PHÂN — earnings {binary} (Sổ 3): "
                     "biến động mạnh quanh giờ ra số")
    for ln in notes.get(t, []):
        parts.append(ln if ln.startswith("·") else "· " + ln)
    return "<br>\n".join(parts) + "</div>"


def rebuild_daytrade(h, prices):
    """Dựng lại khối mã nóng của sec-daytrade từ giá + zones.json
    HIỆN TẠI. Trả (h, số khối). Fail-soft: lỗi thì giữ nguyên mục."""
    try:
        from datetime import timedelta
        b = _sec_bounds(h, "sec-daytrade")
        if not b:
            return h, 0
        s, e = b
        seg = h[s:e]
        zones, _excl = _zones_local()
        open_tag = seg[:seg.find(">") + 1]
        inner = seg[seg.find(">") + 1:-len("</div>")]
        hm = re.search(r"<h2>.*?</h2>", inner, re.S)
        head = inner[:hm.end()] if hm else ""
        rest = inner[hm.end():] if hm else inner
        notes = _day_notes(rest, zones)
        preserved = []
        for s0, s1 in _alert_spans(rest):
            blk = rest[s0:s1]
            if 'data-achip="' not in blk:
                preserved.append(blk)
        miss = [t for t in DAY_HOT_POOL if t not in prices]
        if miss:
            try:
                prices = {**prices, **fetch_pm_prices(miss)}
            except Exception:
                pass
        now = now_pt()
        today_md = (now.month, now.day)
        tmr = now + timedelta(days=1)
        tmr_md = (tmr.month, tmr.day)

        def _binary(t):
            bd = DAY_BINARY.get(t)
            if not bd:
                return None
            if bd == today_md:
                return "hôm nay"
            if bd == tmr_md:
                return "ngày mai"
            return None

        groups = {"EP": [], "BAY": [], "DAP": []}
        for t in DAY_HOT_POOL:
            if t not in prices:
                continue
            px, pc = prices[t]
            z = zones.get(t)
            st = _zone_state_of(z, px) if z and z.get("entry") else None
            grp = key = None
            if st == "IN":
                grp, key = "EP", 0.0
            elif st == "BELOW":
                d = (z["entry"][0] / px - 1) * 100
                if d <= 3.5:
                    grp, key = "EP", d
                elif pc <= -1.5:
                    grp, key = "DAP", pc
            elif st == "ABOVE":
                d = (px / z["entry"][1] - 1) * 100
                if d <= 2.0:
                    grp, key = "EP", d
                elif pc >= 2.0:
                    grp, key = "BAY", -pc
            elif st == "BROKEN":
                if z.get("cut") and px >= z["cut"] * 0.97:
                    grp, key = "EP", 0.5
                else:
                    grp, key = "DAP", pc
            if grp is None and pc >= 2.0:
                grp, key = "BAY", -pc
            elif grp is None and pc <= -1.5:
                grp, key = "DAP", pc
            if grp is None and t in DAY_CORE_KEEP and abs(pc) >= 0.8:
                grp, key = ("BAY", -pc) if pc > 0 else ("DAP", pc)
            if grp:
                groups[grp].append((key, t))
        for g in groups.values():
            g.sort()
        groups["EP"] = groups["EP"][:9]
        groups["BAY"] = groups["BAY"][:8]
        groups["DAP"] = groups["DAP"][:6]
        total = sum(len(g) for g in groups.values())
        if total < 16:  # mâm chưa đủ đầy — bổ sung mã động mạnh nhất còn lại
            have = {t for g in groups.values() for _k, t in g}
            extra = sorted(
                (t for t in DAY_HOT_POOL
                 if t in prices and t not in have),
                key=lambda t: -abs(prices[t][1]))
            for t in extra:
                if total >= 18:
                    break
                px, pc = prices[t]
                g = "BAY" if pc >= 0 else "DAP"
                groups[g].append((0.0, t))
                total += 1
        labels = {"BAY": "🚀 ĐANG BAY", "EP": "🎯 ÉP VÀO VÙNG",
                  "DAP": "🔻 ĐẬP MẠNH / SÁT CẮT"}
        blocks, cnt = [], 0
        for g in ("BAY", "EP", "DAP"):
            if not groups[g]:
                continue
            blocks.append(f'<div style="font-weight:900;margin:10px 0 '
                          f'2px;color:#f5c86e">{labels[g]}</div>')
            for _k, t in groups[g]:
                blocks.append(_day_block(t, prices, zones, notes,
                                         _binary(t)))
                cnt += 1
        if cnt < 8:  # quá ít dữ liệu — không thay mục cũ
            return h, 0
        new_inner = (head + "\n" + "\n".join(preserved)
                     + "\n<!-- DAY-HOT-AUTO -->\n" + "\n".join(blocks)
                     + "\n<!-- /DAY-HOT-AUTO -->")
        return h[:s] + open_tag + new_inner + "</div>" + h[e:], cnt
    except Exception as ex:
        print(f"  ! rebuild daytrade: {ex}", file=sys.stderr)
        return h, 0


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

def steel_net_update(h, prices, highs52, pm=False):
    """Dựng lại section LƯỚI THÉP mỗi lượt — idempotent.
    pm=True: giá là premarket (thanh khoản mỏng) — ghi rõ vào note."""
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
             f'mọi lệnh đều có cắt cứng • cập nhật {ts} PT{" • giá premarket, thanh khoản mỏng" if pm else ""}</div>'
             f"<!-- STEELNET-AUTO -->{body}<!-- /STEELNET-AUTO --></div>\n")
    if 'id="sec-steelnet"' in h:
        h = re.sub(r'<div class="sec" id="sec-steelnet">.*?</div>\n',
                   lambda m: block, h, count=1, flags=re.S)
    else:
        anchor = '<div class="sec" id="sec-chinam">'
        h = h.replace(anchor, block + anchor, 1)
    # nav: thêm 🕸️ Lưới Thép sau 🔪 Săn CM (idempotent — chỉ khi chưa có)
    if 'muc.html?s=sec-steelnet' not in h:
        h = re.sub(r'(href="#sec-sancm">🔪 Săn CM</a>)',
                   r'\1<a href="muc.html?s=sec-steelnet">🕸️ Lưới Thép</a>', h, count=1)
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
    # 30/10: tôn trọng _excluded — mã Tom đã loại khỏi mâm (INTC thesis gãy)
    # không được chui lại qua khối FOCUS (lỗi healthcheck 13:34 PT 30/10).
    try:
        excluded_focus = set(json.load(open(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "zones.json"))).get("_excluded", []))
    except Exception:
        excluded_focus = set()
    rows = []
    for t, tier, ev in SHARK_FOCUS:
        if t in excluded_focus:
            continue
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


def refresh_urgent(h, prices, daylows, events, repo_dir, flows=None, caps=None, revs=None, regime=None):
    date_s = now_pt().strftime("%d/%m")
    # 🧭 LUẬT CHẾ ĐỘ (Tom duyệt 23/10): 🔴 PHÒNG THỦ = TẮT toàn bộ alert mua
    # mới (bounce/capitulation/đảo chiều/dòng tiền/vào vùng đều là tín hiệu
    # mua) — chỉ chèn 1 thông báo phòng thủ mỗi ngày. Tín hiệu vẫn được
    # journal ở tầng detect (chấm điểm chạy tiếp, không gián đoạn số liệu).
    # Cảnh báo GÃY CẮT đi qua add_signals, KHÔNG qua hàm này -> không bị tắt.
    if regime == "RED":
        mark = f"<!-- URG-DEF-{date_s} -->"
        if mark in h:
            return h, "khẩn: 🔴 PHÒNG THỦ — tắt signal mua (đã có thông báo)"
        note = (f'{mark}<div class="alert urgent" data-ts="{now_pt().isoformat(timespec="seconds")}">'
                f'<b>🔴 CHẾ ĐỘ PHÒNG THỦ — HỆ THỐNG ĐÃ TẮT TÍN HIỆU MUA MỚI</b> — '
                f'SPY/QQQ đều dưới MA50, trend đang rơi. Chỉ quản trị vị thế theo cắt cứng, '
                f'canh vùng gom sâu Sổ 1, chờ xác nhận đảo chiều. Bounce/capitulation hôm nay chỉ là tham khảo.<br>'
                f'<span class="t">🧭 Luật Chế Độ • {date_s} • tín hiệu mua bật lại khi SPY/QQQ đòi lại MA20</span></div>\n')
        h2, c1 = re.subn(r'(<div id="urgentBox">)<div class="note">Chưa có tín hiệu khẩn.*?</div>',
                         r"\g<1>" + note, h, count=1, flags=re.S)
        if c1:
            return h2, "khẩn: 🔴 PHÒNG THỦ — đã chèn thông báo tắt signal mua"
        h2, c2 = re.subn(r'(<div id="urgentBox">)', r"\g<1>" + note, h, count=1)
        if c2:
            return h2, "khẩn: 🔴 PHÒNG THỦ — đã chèn thông báo tắt signal mua"
        return h, "khẩn: thiếu #urgentBox (phòng thủ)"
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
    with ThreadPoolExecutor(max_workers=10) as ex:
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


def fetch_daily_closes(ticker):
    """Nến ngày ĐÃ HOÀN TẤT (date < hôm nay PT) từ Yahoo chart API.
    Luật 23/10 (vụ gap sai dấu): sau ~06:30 PT Yahoo thêm nến hôm nay đang
    giao dịch vào series — dcloses[-1] lúc đó là giá live, KHÔNG phải đóng
    cửa. Mọi MA/chế độ phải tính trên nến hoàn tất."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval=1d&range=1y")
    req = Request(url, headers=UA)
    with urlopen(req, timeout=15) as r:
        res = json.load(r)["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    ny = ZoneInfo("America/New_York")
    n = now_pt()
    today = n.date()
    # sau 13:05 PT ngày thường, nến hôm nay đã là giá đóng cửa cuối cùng -> tính luôn
    closed_today = n.weekday() < 5 and (n.hour, n.minute) >= (13, 5)
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t, ny).date()
        if d < today or (closed_today and d == today):
            out.append((d, float(c)))
    return out


def compute_regime(repo_dir):
    """🧭 LUẬT CHẾ ĐỘ — Tom duyệt 23/10 (sau khi soi 1.815 signal: mọi loại
    tín hiệu đều kỳ vọng âm trong tuần trend rơi; con số reversal 30 ngày
    59,7% là ảo, do đúng 1 ngày đảo chiều toàn thị trường 15/10 kéo lên).
    Đèn từ SPY/QQQ vs MA20/50/200 trên nến ngày đã hoàn tất:
      🔴 PHÒNG THỦ: CẢ HAI chỉ số đóng dưới MA50 (trend ngắn/trung đã gãy)
      🟢 TẤN CÔNG: cả hai trên MA50 và MA20
      🟡 TRUNG LẬP: còn lại
    F&G/VIX từ đây chỉ là vai phụ (mùa + độ rẻ), mất quyền cho phép vào lệnh.
    Lỗi mạng/dữ liệu -> giữ state cũ (không bịa đèn)."""
    state_path = os.path.join(repo_dir, "regime_state.json")
    try:
        prev = json.load(open(state_path)) if os.path.exists(state_path) else {}
    except Exception:
        prev = {}
    info = {}
    for tk in ("SPY", "QQQ"):
        try:
            dc = fetch_daily_closes(tk)
        except Exception as e:
            print(f"  ! regime {tk}: {e}", file=sys.stderr)
            return prev
        closes = [c for _, c in dc]
        if len(closes) < 200:
            print(f"  ! regime {tk}: chỉ {len(closes)} nến", file=sys.stderr)
            return prev
        last = closes[-1]
        info[tk] = {
            "close": round(last, 2),
            "ma20": round(sum(closes[-20:]) / 20, 2),
            "ma50": round(sum(closes[-50:]) / 50, 2),
            "ma200": round(sum(closes[-200:]) / 200, 2),
            "day_pc": round((last / closes[-2] - 1) * 100, 2),
            "date": dc[-1][0].isoformat(),
        }
    below50 = all(info[t]["close"] < info[t]["ma50"] for t in info)
    above50 = all(info[t]["close"] > info[t]["ma50"] for t in info)
    above20 = all(info[t]["close"] > info[t]["ma20"] for t in info)
    if below50:
        regime, label = "RED", "🔴 PHÒNG THỦ"
    elif above50 and above20:
        regime, label = "GREEN", "🟢 TẤN CÔNG"
    else:
        regime, label = "YELLOW", "🟡 TRUNG LẬP"
    st = {"regime": regime, "label": label, "indices": info,
          "long_broken": all(info[t]["close"] < info[t]["ma200"] for t in info),
          "updated": now_pt().isoformat(timespec="seconds"),
          "basis": "nến ngày đã hoàn tất gần nhất"}
    try:
        json.dump(st, open(state_path, "w"))
    except Exception as e:
        print(f"  ! regime_state: {e}", file=sys.stderr)
    return st


def render_regime_strip(h, repo_dir):
    """Dải đèn chế độ nằm ngay dưới dải Bull & Bear, đọc regime_state.json."""
    try:
        st = json.load(open(os.path.join(repo_dir, "regime_state.json")))
    except Exception:
        return h
    if not st.get("regime"):
        return h
    i = st.get("indices", {})

    def line(tk):
        d = i.get(tk, {})
        if not d:
            return ""
        return (f'{tk} ${d["close"]:,.2f} · MA20 ${d["ma20"]:,.0f} · '
                f'MA50 ${d["ma50"]:,.0f} · MA200 ${d["ma200"]:,.0f}')
    bg = {"RED": "rgba(220,38,38,.16)", "GREEN": "rgba(22,163,74,.16)",
          "YELLOW": "rgba(234,179,8,.14)"}[st["regime"]]
    if st["regime"] == "RED":
        note = ("Hệ thống TẮT tín hiệu mua mới — chỉ quản trị vị thế theo cắt "
                "cứng, canh vùng gom sâu Sổ 1 và chờ xác nhận đảo chiều "
                "(SPY/QQQ đòi lại MA20). Bounce/capitulation chỉ là tham khảo.")
    elif st["regime"] == "GREEN":
        note = "Trend khỏe — tín hiệu mua hoạt động bình thường theo vùng + cắt cứng."
    else:
        note = ("Chế độ trung lập — tín hiệu mua vẫn chạy nhưng ưu tiên mã "
                "chất lượng, vùng sâu; mã rủi ro đứng ngoài.")
    if st.get("long_broken"):
        note += " ⚠️ Cả hai chỉ số còn dưới MA200 — trend dài hạn cũng đã gãy."
    strip = (f'<div class="regime" id="regimeStrip" style="border:2px solid #f59e0b;'
             f'border-radius:12px;padding:8px 12px;margin:8px 0;background:{bg};font-size:14px">'
             f'<b>🧭 CHẾ ĐỘ THỊ TRƯỜNG — {st["label"]}</b> '
             f'<span style="color:#9fb3d9">{line("SPY")} · {line("QQQ")} · '
             f'theo {st.get("basis", "")} ({i.get("SPY", {}).get("date", "")}) · '
             f'cập nhật {st.get("updated", "")[:16].replace("T", " ")} PT</span><br>'
             f'<span>{note}</span></div>\n')
    h = re.sub(r'<div class="regime" id="regimeStrip".*?</div>\n', "", h, flags=re.S)
    h2, c = re.subn(r'(<div class="bullbear" id="bullBear".*?</div>\n)',
                    r"\g<1>" + strip, h, count=1, flags=re.S)
    if c:
        return h2
    return re.sub(r'(<span class="updated">.*?</span>)', r"\g<1>\n" + strip,
                  h, count=1, flags=re.S)


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


# ================= GỌN TOÀN BẢNG (Tom chốt 31/10/2026) =================
# Tom chê: bảng dài thòng, trùng lặp nhiều, bảng cột vỡ trên phone.
# Bước này chạy CUỐI pipeline (trước stamp): (1) khử trùng alert trong
# từng mục, (2) chip vùng xuyên mục tối đa 2 bản — 1 mã 1 nhà, (3) gỡ
# chip/hàng bảng của mã _excluded, (4) trần khối cho mục tin, (5) thẻ
# LONG TERM gọn: 🟢 chỉ 1 dòng, 🔵/🔴 thẻ hành động tỉa chi tiết + trị
# lặp dòng, (6) dòng tóm ⚡ đầu các mục vùng. Mọi xóa quét CÂN BẰNG.

COMPACT_CAPS = {"sec-swing": 16, "sec-news": 16, "sec-urgent": 20,
                "sec-asia": 12, "sec-hot": 10, "sec-daytrade": 26,
                "sec-live": 12}
# sec-daytrade trần 26 (nâng 04/11 theo lệnh Tom 05:42 — mục daytrade
# phải GIÀU 18–22 khối DAY-HOT + khối tay; trần 12 cũ từ 31/10 cắt mất
# cả nhóm ĐẬP MẠNH ở đuôi mỗi lượt render).
_CHIP_SEC_PRIORITY = ("sec-special", "sec-topfocus", "sec-urgent",
                      "sec-longterm", "sec-so2", "sec-so3", "sec-etf",
                      "sec-etf16", "sec-steelnet", "sec-dram", "sec-watch",
                      "sec-daytrade", "sec-swing", "sec-hot", "sec-live",
                      "sec-news", "sec-asia", "sec-ahshark",
                      "sec-leverage", "sec-chinam", "sec-cudem",
                      "sec-plan", "sec-lt9", "sec-ltalloc")
_SUMMARY_SECS = ("sec-longterm", "sec-so2", "sec-so3", "sec-etf",
                 "sec-etf16", "sec-steelnet", "sec-watch", "sec-dram")

COMPACT_CSS = """<style id="compact-css">
.secsummary{font-weight:700;font-size:14px;background:#131c2e;border:1px solid #27334d;border-radius:8px;padding:6px 10px;margin:6px 0 10px;color:#dbe6f7}
@media (max-width:760px){
.sec table{width:100%}
.sec table tr{display:block;margin:0 0 8px;border:1px solid #232c3d;border-radius:10px;padding:6px 2px;background:#101725}
.sec table th{display:none}
.sec table td{display:block;border:none;padding:2px 8px}
.sec table tr.zchip-row{display:block;border:none;background:none;padding:0;margin:0 0 2px}
.sec table tr.zchip-row td{padding:0}
}
</style>"""


LANG_MARKER = '<!-- LANG-TOGGLE -->'
LANG_BTNS = ('<button class="sharebtn notranslate" type="button" id="langViBtn" onclick="setBoardLang(\'vi\')" title="Tieng Viet"><svg width="20" height="14" viewBox="0 0 30 20" style="vertical-align:-2px;border:1px solid #8a8a8a;border-radius:2px"><rect width="30" height="20" fill="#FFD800"/><rect y="3.5" width="30" height="3" fill="#DA251D"/><rect y="8.5" width="30" height="3" fill="#DA251D"/><rect y="13.5" width="30" height="3" fill="#DA251D"/></svg> VIỆT</button>'
             '<button class="sharebtn notranslate" type="button" id="langEnBtn" onclick="setBoardLang(\'en\')" title="English"><svg width="20" height="14" viewBox="0 0 30 20" style="vertical-align:-2px;border:1px solid #8a8a8a;border-radius:2px"><rect width="30" height="20" fill="#fff"/><rect y="0.00" width="30" height="1.84" fill="#B22234"/><rect y="3.08" width="30" height="1.84" fill="#B22234"/><rect y="6.15" width="30" height="1.84" fill="#B22234"/><rect y="9.23" width="30" height="1.84" fill="#B22234"/><rect y="12.31" width="30" height="1.84" fill="#B22234"/><rect y="15.38" width="30" height="1.84" fill="#B22234"/><rect y="18.46" width="30" height="1.84" fill="#B22234"/><rect width="12" height="10.77" fill="#3C3B6E"/><circle cx="1.0" cy="0.6" r="0.55" fill="#fff"/><circle cx="3.0" cy="0.6" r="0.55" fill="#fff"/><circle cx="5.0" cy="0.6" r="0.55" fill="#fff"/><circle cx="7.0" cy="0.6" r="0.55" fill="#fff"/><circle cx="9.0" cy="0.6" r="0.55" fill="#fff"/><circle cx="11.0" cy="0.6" r="0.55" fill="#fff"/><circle cx="2.0" cy="1.8" r="0.55" fill="#fff"/><circle cx="4.0" cy="1.8" r="0.55" fill="#fff"/><circle cx="6.0" cy="1.8" r="0.55" fill="#fff"/><circle cx="8.0" cy="1.8" r="0.55" fill="#fff"/><circle cx="10.0" cy="1.8" r="0.55" fill="#fff"/><circle cx="1.0" cy="3.0" r="0.55" fill="#fff"/><circle cx="3.0" cy="3.0" r="0.55" fill="#fff"/><circle cx="5.0" cy="3.0" r="0.55" fill="#fff"/><circle cx="7.0" cy="3.0" r="0.55" fill="#fff"/><circle cx="9.0" cy="3.0" r="0.55" fill="#fff"/><circle cx="11.0" cy="3.0" r="0.55" fill="#fff"/><circle cx="2.0" cy="4.2" r="0.55" fill="#fff"/><circle cx="4.0" cy="4.2" r="0.55" fill="#fff"/><circle cx="6.0" cy="4.2" r="0.55" fill="#fff"/><circle cx="8.0" cy="4.2" r="0.55" fill="#fff"/><circle cx="10.0" cy="4.2" r="0.55" fill="#fff"/><circle cx="1.0" cy="5.4" r="0.55" fill="#fff"/><circle cx="3.0" cy="5.4" r="0.55" fill="#fff"/><circle cx="5.0" cy="5.4" r="0.55" fill="#fff"/><circle cx="7.0" cy="5.4" r="0.55" fill="#fff"/><circle cx="9.0" cy="5.4" r="0.55" fill="#fff"/><circle cx="11.0" cy="5.4" r="0.55" fill="#fff"/><circle cx="2.0" cy="6.6" r="0.55" fill="#fff"/><circle cx="4.0" cy="6.6" r="0.55" fill="#fff"/><circle cx="6.0" cy="6.6" r="0.55" fill="#fff"/><circle cx="8.0" cy="6.6" r="0.55" fill="#fff"/><circle cx="10.0" cy="6.6" r="0.55" fill="#fff"/><circle cx="1.0" cy="7.8" r="0.55" fill="#fff"/><circle cx="3.0" cy="7.8" r="0.55" fill="#fff"/><circle cx="5.0" cy="7.8" r="0.55" fill="#fff"/><circle cx="7.0" cy="7.8" r="0.55" fill="#fff"/><circle cx="9.0" cy="7.8" r="0.55" fill="#fff"/><circle cx="11.0" cy="7.8" r="0.55" fill="#fff"/><circle cx="2.0" cy="9.0" r="0.55" fill="#fff"/><circle cx="4.0" cy="9.0" r="0.55" fill="#fff"/><circle cx="6.0" cy="9.0" r="0.55" fill="#fff"/><circle cx="8.0" cy="9.0" r="0.55" fill="#fff"/><circle cx="10.0" cy="9.0" r="0.55" fill="#fff"/><circle cx="1.0" cy="10.2" r="0.55" fill="#fff"/><circle cx="3.0" cy="10.2" r="0.55" fill="#fff"/><circle cx="5.0" cy="10.2" r="0.55" fill="#fff"/><circle cx="7.0" cy="10.2" r="0.55" fill="#fff"/><circle cx="9.0" cy="10.2" r="0.55" fill="#fff"/><circle cx="11.0" cy="10.2" r="0.55" fill="#fff"/></svg> ENGLISH</button>')
LANG_SNIPPET = """<!-- LANG-TOGGLE -->
<div id="google_translate_element" style="display:none"></div>
<script>
function googleTranslateElementInit(){try{new google.translate.TranslateElement({pageLanguage:'vi',includedLanguages:'en,vi',autoDisplay:false},'google_translate_element');}catch(e){}setTimeout(applyCookieLang,900);killGoogleBar();}
function killGoogleBar(){try{document.querySelectorAll('.goog-te-banner-frame').forEach(function(f){f.style.setProperty('display','none','important');if(f.parentNode)f.parentNode.removeChild(f);});if(document.body)document.body.style.setProperty('top','0px','important');}catch(e){}}
setInterval(killGoogleBar,700);
function applyCookieLang(){try{if(document.cookie.indexOf('googtrans=/vi/en')>-1){var c=document.querySelector('.goog-te-combo');if(c&&c.value!=='en'){c.value='en';c.dispatchEvent(new Event('change'));}}}catch(e){}}
function setBoardLang(l){var past='expires=Thu, 01 Jan 1970 00:00:00 GMT';if(l==='en'){var d=new Date(Date.now()+365*864e5).toUTCString();document.cookie='googtrans=/vi/en; path=/; expires='+d;document.cookie='googtrans=/vi/en; path=/bang-market-live/; expires='+d;}else{document.cookie='googtrans=; path=/; '+past;document.cookie='googtrans=; path=/bang-market-live/; '+past;}location.reload();}/*LANG-RELOAD-FIX*/
function markLangBtn(l){try{var a=document.getElementById('langViBtn'),b=document.getElementById('langEnBtn');if(a){a.style.background='';a.style.color='';}if(b){b.style.background='';b.style.color='';}var on=document.getElementById(l==='en'?'langEnBtn':'langViBtn');if(on){on.style.background='#b45309';on.style.color='#fff';}}catch(e){}}
document.addEventListener('DOMContentLoaded',function(){markLangBtn(document.cookie.indexOf('googtrans=/vi/en')>-1?'en':'vi');killGoogleBar();});
</script>
<script src="https://translate.google.com/translate_a/element.js?cb=googleTranslateElementInit"></script>
<style>.goog-te-banner-frame,iframe.goog-te-banner-frame{display:none!important;visibility:hidden!important;height:0!important;max-height:0!important}body{top:0!important}.goog-te-gadget{display:none!important}#goog-gt-tt,.goog-te-balloon-frame{display:none!important}.goog-text-highlight{background:transparent!important;box-shadow:none!important}</style>
"""

def ensure_lang_toggle(html):
    """Ban song ngu dung Google Translate widget (Tom 1/11 chot lai: ban Anh tu
    dich chua sach nen tam dung Google; en.html van duy tri nen de sau nay thay).
    Tu gan lai snippet neu mot generator lam mat."""
    if 'googleTranslateElementInit' not in html and '</body>' in html:
        html = html.replace('</body>', LANG_SNIPPET + '\n</body>', 1)
    return html

def ensure_share_gate(html):
    """Khoa mat khau rieng cho muc chia se (Tom 1/11): con mat + nut Chia se
    chi mo khi nhap dung mat khau chu bang; tu gan lai neu generator lam mat."""
    if 'SHARE-GATE' in html:
        return html
    try:
        import importlib.util, os
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'patch_share_gate.py')
        spec = importlib.util.spec_from_file_location('patch_share_gate', p)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        if mod.OLD_BLOCK in html:
            html = html.replace(mod.OLD_BLOCK, mod.NEW_BLOCK, 1)
            if mod.OLD_TITLE in html:
                html = html.replace(mod.OLD_TITLE, mod.NEW_TITLE, 1)
    except Exception:
        pass
    return html

YAHOO_LIVE_SNIPPET = """
<!-- YAHOO-LIVE-V9 -->
<script>
(function(){
function fmtP(x){var s=x.toLocaleString('vi-VN',{minimumFractionDigits:2,maximumFractionDigits:2});if(s.slice(-3)===',00')s=s.slice(0,-3);else if(s.indexOf(',')>=0&&s.slice(-1)==='0')s=s.slice(0,-1);return '$'+s;}
function fmtPc(x){var s=Math.abs(x).toFixed(2).replace('.',',');return (x<0?'−':x>0?'+':'')+s+'%';}
function fmtUS(x){return x.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}
function fmtPct1(x){return (x<0?'−':x>0?'+':'')+Math.abs(x).toFixed(1)+'%';}
function fmtCg(p){if(p>=1000)return '$'+p.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});if(p>=1)return '$'+p.toFixed(3);if(p>=0.01)return '$'+p.toFixed(4);return '$'+p.toFixed(6);}
function tick(){
fetch('prices.json?cb='+Date.now()).then(function(r){return r.json();}).then(function(j){
var q=j.q||{};
function wsf(t){return window.__wsFresh&&window.__wsFresh[t]&&(Date.now()-window.__wsFresh[t]<90000);}
document.querySelectorAll('[data-lchip]').forEach(function(e){var t=e.getAttribute('data-lchip'),d=q[t];if(!d||wsf(t))return;e.textContent=e.textContent.replace(/^\\S+ \\$[\\d.,]+ \\([^)]*\\)/,function(){return t+' '+fmtP(d[0])+' ('+fmtPc(d[1])+')';});});
document.querySelectorAll('[data-achip],[data-fchip]').forEach(function(e){var t=e.getAttribute('data-achip')||e.getAttribute('data-fchip'),d=q[t];if(!d||wsf(t))return;e.textContent=e.textContent.replace(/(Giá )\\$[\\d.,]+/,function(m,g){return g+fmtP(d[0]);});});
document.querySelectorAll('tr.zchip-row').forEach(function(tr){var _t=tr.getAttribute('data-t'),d=q[_t];if(!d||wsf(_t))return;var c=tr.querySelector('.zchip');if(!c)return;c.textContent=c.textContent.replace(/(Giá )\\$[\\d.,]+/,function(m,g){return g+fmtP(d[0]);});});
document.querySelectorAll('.livechip').forEach(function(c){var lt=c.querySelector('.lt'),lp=c.querySelector('.lp'),lc=c.querySelector('.lc');if(!lt||!lp)return;var mt=lt.textContent.trim().match(/^[A-Z]+/);if(!mt)return;var d=q[mt[0]];if(!d||wsf(mt[0]))return;lp.textContent=fmtUS(d[0]);if(lc){var up=d[1]>=0;lc.textContent=(up?'▲ ':'▼ ')+Math.abs(d[1]).toFixed(2)+'%';lc.className='lc '+(up?'up':'down');}});
document.querySelectorAll('tr').forEach(function(tr){var b=tr.querySelector('td b');if(!b)return;var t=b.textContent.trim();if(!/^[A-Z]{1,6}$/.test(t))return;var d=q[t];if(!d||wsf(t))return;var td=tr.children[1];if(!td)return;var m=td.innerHTML.match(/^((?:~?\\$)?)[\\d,]+(?:\\.\\d+)?(\\s*<span class=")(down|up)(">)[^<]*(<\\/span>)/);if(m){td.innerHTML=m[1]+fmtUS(d[0])+m[2]+(d[1]<0?'down':'up')+m[4]+fmtPct1(d[1])+m[5];}else if(/^\\s*~?\\$[\\d,]+(?:\\.\\d+)?\\s*$/.test(td.textContent)){td.textContent=td.textContent.replace(/[\\d,]+(?:\\.\\d+)?/,fmtUS(d[0]));}});
document.querySelectorAll('b').forEach(function(b){if(!b.closest('#sec-etf,#sec-ltalloc,#sec-lt9'))return;var t=b.textContent.trim();if(!/^[A-Z]{1,6}$/.test(t))return;var d=q[t];if(!d||wsf(t))return;var ns=b.nextSibling;if(!ns||ns.nodeType!==3)return;if(/^\\s*—\\s*\\$[\\d.,]+ \\([^)]*\\)/.test(ns.textContent)){ns.textContent=ns.textContent.replace(/^(\\s*—\\s*)\\$[\\d.,]+ \\([^)]*\\)/,function(m,g){return g+fmtP(d[0])+' ('+fmtPc(d[1])+')';});}});
document.querySelectorAll('[id^="lta-chip-"]').forEach(function(e){var _t=e.id.replace('lta-chip-',''),d=q[_t];if(!d||wsf(_t))return;e.textContent=e.textContent.replace(/(Giá )\\$[\\d.,]+/,function(m,g){return g+fmtP(d[0]);});});
document.querySelectorAll('.levcomp').forEach(function(e){var m=e.textContent.match(/: ([A-Z]{2,6}) /);if(!m)return;var d=q[m[1]];if(!d||wsf(m[1]))return;e.textContent=e.textContent.replace(/(: [A-Z]{2,6} )\\$[\\d.,]+ \\([^)]*\\)/,function(m,g){return g+fmtP(d[0])+' ('+fmtPc(d[1])+')';});});
if(j.cg){window.__yhOk=true;document.querySelectorAll('[data-cgprice]').forEach(function(e){var d=j.cg[e.getAttribute('data-cgprice')];if(d)e.textContent=fmtCg(d[0]);});document.querySelectorAll('[data-cgchg]').forEach(function(e){var d=j.cg[e.getAttribute('data-cgchg')];if(d)e.textContent=(d[1]>=0?'+':'')+d[1].toFixed(1)+'%';});var cw=document.getElementById('cgLiveWhen');if(cw){var age2=j.ts_epoch?Math.max(0,Math.round(Date.now()/1000-j.ts_epoch)):null;cw.textContent=age2===null?' · 🟢 LIVE':(age2<=180?' · 🟢 LIVE · giá '+age2+' giây trước':' · 🟡 GIÁ CŨ '+Math.round(age2/60)+' phút');}}
var lamp=document.getElementById('yhLiveWhen');if(lamp&&!(window.__wsLast&&(Date.now()-window.__wsLast<30000))){var age=j.ts_epoch?Math.max(0,Math.round(Date.now()/1000-j.ts_epoch)):null;if(age===null){lamp.textContent='· 🟢 YAHOO LIVE';}else if(age<=180){lamp.textContent='· 🟢 YAHOO LIVE · giá '+age+' giây trước';}else{lamp.textContent='· 🟡 GIÁ CŨ '+Math.round(age/60)+' phút — chờ cập nhật';}}
}).catch(function(){});
}
tick();setInterval(tick,30000);
})();
</script>
"""


def ensure_yahoo_live(html):
    """YAHOO LIVE (Tom 1/11): may poller trong trang tu cap nhat gia Yahoo
    tren cac chip moi 30 giay tu prices.json cung nguon (Yahoo chan CORS
    nhung truc tiep nen phai qua cau noi nay — xem yahoo_live.py).
    Tu gan lai snippet + den LIVE neu generator lam mat."""
    if 'YAHOO-LIVE-V9' not in html:
        i = html.find('<!-- YAHOO-LIVE')
        if i != -1:
            j = html.find('</script>', i)
            if j != -1:
                html = html[:i] + html[j + 9:]
        if '</body>' in html:
            html = html.replace('</body>', YAHOO_LIVE_SNIPPET + '\n</body>', 1)
    if 'id="yhLiveWhen"' not in html:
        i = html.find('id="sec-live"')
        if i != -1:
            j = html.find('</h2>', i)
            if j != -1 and j - i < 800:
                html = (html[:j]
                        + ' <span id="yhLiveWhen" style="font-size:12px;'
                          'color:#4ade80;font-weight:400">· '
                          '\\U0001F7E2 YAHOO LIVE</span>'
                        + html[j:])
    return html


WS_LIVE_SNIPPET = r"""
<!-- YAHOO-WS-V3 -->
<script>
(function(){
if(window.__wsStarted)return;window.__wsStarted=true;
window.__wsFresh=window.__wsFresh||{};window.__wsLast=0;
function fmtP(x){var s=x.toLocaleString('vi-VN',{minimumFractionDigits:2,maximumFractionDigits:2});if(s.slice(-3)===',00')s=s.slice(0,-3);else if(s.indexOf(',')>=0&&s.slice(-1)==='0')s=s.slice(0,-1);return '$'+s;}
function fmtPc(x){var s=Math.abs(x).toFixed(2).replace('.',',');return (x<0?'−':x>0?'+':'')+s+'%';}
function fmtUS(x){return x.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}
function fmtPct1(x){return (x<0?'−':x>0?'+':'')+Math.abs(x).toFixed(1)+'%';}
function mf(v){var s=String(Math.round(v*100)/100),p=s.split('.');p[0]=p[0].replace(/\B(?=(\d{3})+(?!\d))/g,'.');if(p.length>1)p[1]=(p[1]+'0').slice(0,2);return '$'+p.join(',');}
function b64b(b64){var bin=atob(b64),a=new Uint8Array(bin.length);for(var i=0;i<bin.length;i++)a[i]=bin.charCodeAt(i);return a;}
function dec(buf){var o={},pos=0,dv=new DataView(buf.buffer,buf.byteOffset,buf.byteLength);function vi(){var sh=0,r=0;while(true){var b=buf[pos++];r+=(b&0x7f)*Math.pow(2,sh);if(!(b&0x80))break;sh+=7;}return r;}while(pos<buf.length){var key=vi(),f=Math.floor(key/8),wt=key%8;if(wt===0){var v=vi();if(f===3)o.time=v;}else if(wt===5){var fl=dv.getFloat32(pos,true);pos+=4;if(f===2)o.price=fl;if(f===8)o.cp=fl;if(f===12)o.ch=fl;}else if(wt===1){pos+=8;}else if(wt===2){var ln=vi();if(f===1)o.id=new TextDecoder().decode(buf.slice(pos,pos+ln));pos+=ln;}else break;}return o;}
var seen={},syms=[];
document.querySelectorAll('[data-lchip],[data-achip],[data-fchip],[data-px],[data-pct]').forEach(function(e){var t=e.getAttribute('data-lchip')||e.getAttribute('data-achip')||e.getAttribute('data-fchip')||e.getAttribute('data-px')||e.getAttribute('data-pct');if(t&&/^[A-Z]{1,6}$/.test(t)&&!seen[t]){seen[t]=1;syms.push(t);}});
document.querySelectorAll('tr.zchip-row').forEach(function(e){var t=e.getAttribute('data-t');if(t&&/^[A-Z]{1,6}$/.test(t)&&!seen[t]){seen[t]=1;syms.push(t);}});
document.querySelectorAll('tr').forEach(function(e){var b=e.querySelector('td b');if(!b)return;var t=b.textContent.trim();if(/^[A-Z]{1,6}$/.test(t)&&!seen[t]){seen[t]=1;syms.push(t);}});
document.querySelectorAll('[id^="lta-chip-"]').forEach(function(e){var t=e.id.replace('lta-chip-','');if(/^[A-Z]{1,6}$/.test(t)&&!seen[t]){seen[t]=1;syms.push(t);}});
document.querySelectorAll('.livechip').forEach(function(e){var lt=e.querySelector('.lt');if(!lt)return;var m=lt.textContent.trim().match(/^[A-Z]{1,6}/);if(m&&!seen[m[0]]){seen[m[0]]=1;syms.push(m[0]);}});
document.querySelectorAll('.levcomp').forEach(function(e){var m=e.textContent.match(/: ([A-Z]{2,6}) /);if(m&&!seen[m[1]]){seen[m[1]]=1;syms.push(m[1]);}});
['SPY','QQQ'].forEach(function(t){if(!seen[t]){seen[t]=1;syms.push(t);}});
var pending={},flushT=null,lampT=0;
function apply(id,d){
document.querySelectorAll('[data-lchip="'+id+'"]').forEach(function(e){e.textContent=e.textContent.replace(/^\S+ \$[\d.,]+ \([^)]*\)/,function(){return id+' '+fmtP(d.price)+' ('+fmtPc(d.cp||0)+')';});});
document.querySelectorAll('[data-achip="'+id+'"],[data-fchip="'+id+'"]').forEach(function(e){e.textContent=e.textContent.replace(/(Giá )\$[\d.,]+/,function(m,g){return g+fmtP(d.price);});});
var lc=document.getElementById('lta-chip-'+id);if(lc){lc.textContent=lc.textContent.replace(/(Giá )\$[\d.,]+/,function(m,g){return g+fmtP(d.price);});}
document.querySelectorAll('[data-px="'+id+'"]').forEach(function(e){e.textContent=id+' '+mf(d.price);});
document.querySelectorAll('[data-pct="'+id+'"]').forEach(function(e){var c=d.cp||0;e.textContent='('+(c>0?'+':'')+c.toFixed(2).replace('.',',')+'%)';});
document.querySelectorAll('tr.zchip-row[data-t="'+id+'"]').forEach(function(tr){var c=tr.querySelector('.zchip');if(c)c.textContent=c.textContent.replace(/(Giá )\$[\d.,]+/,function(m,g){return g+fmtP(d.price);});});
document.querySelectorAll('tr').forEach(function(tr){var b=tr.querySelector('td b');if(!b||b.textContent.trim()!==id)return;var td=tr.children[1];if(!td)return;var m=td.innerHTML.match(/^((?:~?\$)?)[\d,]+(?:\.\d+)?(\s*<span class=")(down|up)(">)[^<]*(<\/span>)/);if(m){td.innerHTML=m[1]+fmtUS(d.price)+m[2]+((d.cp||0)<0?'down':'up')+m[4]+fmtPct1(d.cp||0)+m[5];}else if(/^\s*~?\$[\d,]+(?:\.\d+)?\s*$/.test(td.textContent)){td.textContent=td.textContent.replace(/[\d,]+(?:\.\d+)?/,fmtUS(d.price));}});
document.querySelectorAll('.livechip').forEach(function(c){var lt=c.querySelector('.lt'),lp=c.querySelector('.lp'),lc=c.querySelector('.lc');if(!lt||!lp)return;var mt=lt.textContent.trim().match(/^[A-Z]{1,6}/);if(!mt||mt[0]!==id)return;lp.textContent=d.price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});if(lc){var cp=d.cp||0;lc.textContent=(cp>=0?'▲ ':'▼ ')+Math.abs(cp).toFixed(2)+'%';lc.className='lc '+(cp>=0?'up':'down');}});
document.querySelectorAll('.levcomp').forEach(function(e){var m=e.textContent.match(/: ([A-Z]{2,6}) /);if(!m||m[1]!==id)return;e.textContent=e.textContent.replace(/(: [A-Z]{2,6} )\$[\d.,]+ \([^)]*\)/,function(mm,g){return g+fmtP(d.price)+' ('+fmtPc(d.cp||0)+')';});});
}
function flush(){var k;for(k in pending)apply(k,pending[k]);pending={};var now=Date.now();if(now-lampT>4000){lampT=now;var lamp=document.getElementById('yhLiveWhen');if(lamp)lamp.textContent='· ⚡ REALTIME '+new Date().toLocaleTimeString('en-GB');}}
function onTick(d){if(!d.id||d.price===undefined)return;window.__wsFresh[d.id]=Date.now();window.__wsLast=Date.now();if(d.id==='BTC-USD')return;pending[d.id]=d;if(!flushT)flushT=setTimeout(function(){flushT=null;flush();},250);}
var ws=null,backoff=3000,ka=null;
function subscribe(){if(!ws||ws.readyState!==1)return;var all=syms.concat(['BTC-USD']);for(var i=0;i<all.length;i+=60){try{ws.send(JSON.stringify({subscribe:all.slice(i,i+60)}));}catch(e){}}}
function connect(){try{ws=new WebSocket('wss://streamer.finance.yahoo.com/?version=2');}catch(e){retry();return;}
ws.onopen=function(){backoff=3000;subscribe();if(ka)clearInterval(ka);ka=setInterval(subscribe,25000);};
ws.onmessage=function(ev){try{var b64=null;try{var j=JSON.parse(ev.data);if(j&&j.message)b64=j.message;}catch(e){}if(!b64)b64=ev.data;onTick(dec(b64b(b64)));}catch(e){}};
ws.onclose=function(){retry();};ws.onerror=function(){try{ws.close();}catch(e){}};}
function retry(){if(ka){clearInterval(ka);ka=null;}window.__wsFresh={};setTimeout(connect,backoff);backoff=Math.min(backoff*2,30000);}
connect();
})();
</script>
"""


def ensure_ws_live(html):
    """YAHOO STREAMER (Tom 2/11 — đột phá realtime): máy khách WebSocket
    ngay trong trang nối thẳng wss://streamer.finance.yahoo.com (đường ống
    của chính Yahoo Finance live; test trình duyệt thật trên đúng origin
    github.io 2/11 21:22 PT: 20 tick/48s BTC+ETH, giá khớp). Tick về là
    cập nhật chip giá ngay; poller prices.json (5 phút) thành dự phòng —
    khóa wsf() trong poller cũ chặn ghi đè mã đang có tick tươi <90s.
    BTC-USD chỉ làm nhịp tim giữ đèn ⚡ (giá crypto vẫn do CoinGecko lo,
    % 24h thật). Tự gắn lại snippet nếu generator làm mất."""
    if 'YAHOO-WS-V3' not in html and '</body>' in html:
        i = html.find('<!-- YAHOO-WS')
        if i != -1:
            j = html.find('</script>', i)
            if j != -1:
                html = html[:i] + html[j + 9:]
        html = html.replace('</body>', WS_LIVE_SNIPPET + '\n</body>', 1)
    return html


def write_prices_cache(repo_dir, prices):
    """Ghi prices_cache.json cho yahoo_live.py dung lai (tranh fetch lai).
    Fail-soft: loi thi bo qua, yahoo_live tu fetch bo sung."""
    try:
        out = {}
        for t, v in (prices or {}).items():
            if isinstance(v, (list, tuple)) and len(v) >= 2 \
                    and isinstance(v[0], (int, float)):
                out[t] = [round(float(v[0]), 4), round(float(v[1]), 2)]
        if out:
            with open(os.path.join(repo_dir, "prices_cache.json"), "w",
                      encoding="utf-8") as f:
                json.dump(out, f, separators=(",", ":"))
    except Exception:
        pass


def publish_yahoo_live():
    """Bom prices.json (Yahoo) sau moi luot refresh — fail-soft, khong chan bang."""
    try:
        import subprocess as _sp
        _sp.run([sys.executable,
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "yahoo_live.py")],
                capture_output=True, text=True, timeout=300)
    except Exception:
        pass

def ensure_compact_css(h):
    if 'id="compact-css"' in h:
        return h
    return h.replace("</head>", COMPACT_CSS + "</head>", 1)


def _sec_span_by_id(h, sec_id):
    i = h.find(f'id="{sec_id}"')
    if i == -1:
        return None
    return _sec_span_containing(h, i)


def _chip_alert_key(full):
    """Alert 'chip vùng' (bản sao trạng thái vùng của 1 mã, do
    update_alert_chips nhân ra nhiều mục) -> (mã, chữ ký). Alert tin
    tức kể chuyện KHÔNG phải chip -> None, tuyệt đối không đụng."""
    cm = re.search(r'<div class="zchip[^"]*" data-achip="([A-Z]{2,6})">'
                   r'(.*?)</div>', full, re.S)
    if cm:
        # Chữ ký tính trên RIÊNG khung chip (không tính phần tin đi kèm):
        # cùng 1 trạng thái vùng nhân ra 5 mục vẫn là 1 sự kiện.
        return (cm.group(1), _alert_sig(cm.group(0), cm.group(1)))
    if "data-achip=" in full:
        t = _alert_ticker(full)
        return (t, _alert_sig(full, t)) if t else None
    txt = re.sub(r"<[^>]+>", " ", full)
    txt = re.sub(r"\s+", " ", txt).strip()
    if re.match(r"^[A-Z]{2,6} · ", txt) and "Vùng" in txt and "Cắt" in txt:
        t = _alert_ticker(full)
        return (t, _alert_sig(full, t)) if t else None
    return None


def _compact_chip_dedup(h, excluded):
    """Chip vùng của cùng 1 mã + cùng trạng thái: giữ tối đa 2 bản trên
    toàn bảng (ưu tiên mục đặc biệt/topfocus/khẩn rồi tới sổ nhà); chip
    của mã _excluded: gỡ sạch (mã đã loại khỏi mâm — vd INTC 30/10)."""
    counts = {}
    for sec_id in _CHIP_SEC_PRIORITY:
        span = _sec_span_by_id(h, sec_id)
        if not span:
            continue
        s0, s1 = span
        seen_here = set()

        def fn(full, inner):
            key = _chip_alert_key(full)
            if key is None:
                return full
            if key[0] in excluded:
                # Gỡ riêng khung chip của mã đã loại; phần tin kể chuyện
                # đi kèm (nếu còn đáng kể) thì giữ lại, không xóa oan.
                stripped = re.sub(
                    r'<div class="zchip[^"]*" data-achip="' + key[0]
                    + r'">.*?</div>', '', full, flags=re.S)
                txt = re.sub(r"\s+", " ",
                             re.sub(r"<[^>]+>", " ", stripped)).strip()
                return stripped if len(txt) >= 30 else ""
            # FIX 02/11: cùng 1 chip xuất hiện 2 lần trong CÙNG 1 mục = trùng
            # hiển thị (cap toàn bảng 2 bản là cho 2 mục khác nhau).
            if key in seen_here:
                return ""
            seen_here.add(key)
            c = counts.get(key, 0)
            if c >= 2:
                return ""
            counts[key] = c + 1
            return full

        seg2 = sub_balanced_alerts(h[s0:s1], fn)
        h = h[:s0] + seg2 + h[s1:]
    return h


def _compact_excluded_rows(h, excluded):
    """Gỡ cặp hàng (chip + chi tiết) của mã _excluded khỏi các bảng vùng."""
    for t in excluded:
        pat = re.compile(
            r'<tr class="zchip-row" data-t="' + t + r'"><td colspan="\d+">'
            r'.*?</td></tr>\s*<tr><td><b>' + t + r'</b></td>.*?</tr>', re.S)
        h = pat.sub('', h)
        pat2 = re.compile(
            r'<tr class="zchip-row" data-t="' + t + r'"><td colspan="\d+">'
            r'.*?</td></tr>', re.S)
        h = pat2.sub('', h)
    return h


def _compact_caps(h):
    """Trần khối cho các mục tin/tín hiệu: giữ các khối đầu (mới nhất /
    hành động xếp trước sẵn), gỡ phần đuôi dài. Khối cấu trúc miễn trừ."""
    for sec_id, cap in COMPACT_CAPS.items():
        span = _sec_span_by_id(h, sec_id)
        if not span:
            continue
        s0, s1 = span
        state = {"n": 0}

        def fn(full, inner):
            head = full.split(">", 1)[0]
            if (any(k in inner for k in _STRUCT_KEEP)
                    or 'lta-row' in head or 'secsummary' in full):
                return full
            state["n"] += 1
            return full if state["n"] <= cap else ""

        seg2 = sub_balanced_alerts(h[s0:s1], fn)
        h = h[:s0] + seg2 + h[s1:]
    return h


def _lta_clean_tail(tail):
    """Bỏ các cặp dòng vòng 'Mua ~.../Còn...' mồ côi + </span> lạc nằm
    SAU span zone (di tích bug lặp dòng ltalloc trước 31/10)."""
    tail = re.sub(r'(<br>\s*)?Mua ~[^<]*<br>\s*(Còn|Rớt) [^<]*', '', tail)
    tail = tail.replace('</span>', '')
    return tail


def _compact_ltalloc(h):
    """Thẻ LONG TERM gọn (ý Tom duyệt 31/10): quỹ 🔵/🔴 = thẻ hành động
    (chip + tên + dải rút gọn 1 cặp dòng + lý do chọn); quỹ 🟢/két =
    đúng 1 dòng (chip + khoảng cách tới mốc đổi màu). Xếp thẻ hành
    động lên trước. Giữ nguyên id lta-chip/lta-zone/lta-ret để pipeline cập
    nhật giá realtime + lãi ngày/tháng/năm không gãy (bài học 3/11: bản gọn
    cũ nuốt mất span lta-ret → job ltalloc-returns-daily fail liên tiếp)."""
    span = _sec_span_by_id(h, "sec-ltalloc")
    if not span:
        return h
    s0, s1 = span
    seg = h[s0:s1]

    def fn(full, inner):
        m = re.search(r'id="lta-row-([A-Z]{2,6})"', full)
        if not m:
            return full
        t = m.group(1)
        # Giữ span lãi (lta-ret) để job returns-daily còn chỗ ghi — bản gọn
        # cũ nuốt mất span này nên updater fail 3 lượt liên tiếp (3/11).
        ret = ''
        rm = re.search(r'<span id="lta-ret-' + t + r'">[^<]*</span>', full)
        if rm:
            ret = rm.group(0)
        open_tag = full[:full.find('>') + 1]
        chip_m = re.search(r'<div class="lta-chip[^"]*" id="lta-chip-'
                           + t + r'">.*?</div>', full, re.S)
        chip = chip_m.group(0) if chip_m else ''
        zm = re.search(r'<span id="lta-zone-' + t + r'"[^>]*>', full)
        zone_open, zone_inner, tail = '', '', full
        if zm:
            zend = _balanced_span_end(full, zm.start())
            zone_open = zm.group(0)
            zone_inner = full[zm.end():zend - 7] if zend >= zm.end() + 7 else ''
            tail = full[zend:]
        tail = _lta_clean_tail(tail)
        is_action = ('lta-row-r' in open_tag) or ('lta-row-b' in open_tag)
        if not is_action:
            if t == 'SGOV' or not chip:
                body = (f'<b>{t}</b> — 💰 KÉT TIỀN — tiền 🔴 bán bớt về '
                        'đây nằm chờ; quỹ nào rớt 🔵 thì lấy ra mua nhiều thêm.')
                if zone_open:
                    body += ' ' + zone_open + 'Két không mua bán theo giá.</span>'
                return open_tag + body + ('<br>' + ret if ret else '') + '</div>'
            dist = ''
            txt = re.sub(r'<[^>]+>', ' ', zone_inner)
            dm = re.search(r'(Còn [^<]{5,90}?sang (?:🔵|🔴)[^<]{0,40})', txt)
            if dm:
                dist = re.sub(r'\s+', ' ', dm.group(1)).strip()
            line = chip
            if zone_open:
                line += (' ' + zone_open
                         + (dist or '🟢 Mua định kỳ theo kỳ nạp.') + '</span>')
            return open_tag + line + ('<br>' + ret if ret else '') + '</div>'
        # Thẻ hành động: tỉa thanh %, dòng vốn/nạp, dòng lãi giá.
        name = ''
        nm = re.search(r'(<b>' + t + r'</b>.*?)(?=<div style="background|💰'
                       r'|<span id="lta-zone)', full, re.S)
        if nm:
            name = nm.group(1)
        znew = ''
        if zone_open:
            cut = zone_inner.find('Còn ')
            if cut != -1:
                endline = zone_inner.find('<', cut)
                zcontent = zone_inner[:endline if endline != -1
                                      else len(zone_inner)]
            else:
                zcontent = zone_inner
            znew = zone_open + zcontent + '</span>'
        why = ''
        wm = re.search(r'🎯 Vì sao chọn:[^<]*', tail)
        if wm:
            why = '<br>' + wm.group(0).strip()
        return open_tag + chip + name + znew + why + ('<br>' + ret if ret else '') + '</div>'

    seg = sub_balanced_alerts(seg, fn)
    # Xếp lại: thẻ hành động lên trước các dòng định kỳ (khối lta-row
    # liên tiếp nhau, chỉ cách nhau khoảng trắng).
    rows = []
    pat = re.compile(r'<div class="alert lta-row[^>]*>')
    pos = []
    i = 0
    while True:
        mm = pat.search(seg, i)
        if not mm:
            break
        depth, j = 0, mm.start()
        while True:
            o = seg.find('<div', j)
            c = seg.find('</div>', j)
            if c == -1:
                j = len(seg)
                break
            if o != -1 and o < c:
                depth += 1
                j = o + 4
            else:
                depth -= 1
                j = c + 6
                if depth == 0:
                    break
        pos.append((mm.start(), j))
        i = j
    if len(pos) >= 2:
        contiguous = all(seg[pos[k][1]:pos[k + 1][0]].strip() == ''
                         for k in range(len(pos) - 1))
        if contiguous:
            blocks = [seg[a:b] for a, b in pos]
            blocks.sort(key=lambda b: 0 if ('lta-row-r' in b[:80]
                                            or 'lta-row-b' in b[:80]) else 1)
            seg = seg[:pos[0][0]] + ''.join(blocks) + seg[pos[-1][1]:]
    return h[:s0] + seg + h[s1:]


def _compact_summary(h, sec_id):
    """Dòng tóm ⚡ đầu mục vùng: đếm trạng thái từ chip trong mục —
    mở mục 3 giây là nắm tình hình (Tom lệnh 31/10)."""
    span = _sec_span_by_id(h, sec_id)
    if not span:
        return h
    s0, s1 = span
    seg = h[s0:s1]
    # FIX 02/11: đếm theo token đầy đủ 'zchip zchip-X' — bản cũ đếm 'zchip-r'
    # dính cả class 'zchip-row' của mọi hàng bảng → số 🔴 phình gấp nhiều lần.
    g = seg.count('zchip zchip-g')
    y = seg.count('zchip zchip-y')
    r = seg.count('zchip zchip-r')
    n = seg.count('zchip zchip-n')
    if g + y + r + n == 0:
        return h
    parts = []
    if g:
        parts.append(f"🟢 {g} trong vùng")
    if y:
        parts.append(f"🟡 {y} dưới vùng")
    if r:
        parts.append(f"🔴 {r} gãy cắt")
    if n:
        parts.append(f"⚪ {n} trên vùng")
    line = ('<div class="secsummary">⚡ TRONG MỤC: '
            + ' · '.join(parts) + '</div>')
    if 'secsummary' in seg:
        seg = re.sub(r'<div class="secsummary">.*?</div>', line, seg, count=1)
    elif '</h2>' in seg:
        seg = seg.replace('</h2>', '</h2>' + line, 1)
    else:
        return h
    return h[:s0] + seg + h[s1:]


def _sort_ltalloc_actions(h):
    """Quỹ LONG TERM có tín hiệu HÀNH ĐỘNG (hàng mang class lta-row-r =
    🔴 BÁN BỚT / lta-row-b = 🔵 MUA NHIỀU) tự nổi lên ĐẦU nhóm hàng quỹ
    trong sec-ltalloc (luật Tom 31/10: có tín hiệu mua/bán là phải show
    lên trên). 🟢/két giữ nguyên thứ tự phân bổ. Chỉ hoán đổi vị trí các
    khối hàng quỹ với nhau (stack-scan cân bằng), không đụng nội dung."""
    i = h.find('id="sec-ltalloc"')
    if i == -1:
        return h
    j = h.find('id="sec-lt9"', i)
    if j == -1:
        j = min(len(h), i + 60000)
    seg = h[i:j]
    starts = [m for m in re.finditer(
        r'<div class="alert lta-row[^"]*" id="lta-row-[A-Z]+"', seg)]
    if len(starts) < 2:
        return h
    blocks = []
    for m in starts:
        end = _levcomp_balanced_end(seg, m.start())
        if end is None:
            return h
        blocks.append((m.start(), end, seg[m.start():end]))

    def rank(b):
        if 'lta-row-r' in b[2][:120]:
            return 0
        if 'lta-row-b' in b[2][:120]:
            return 1
        return 2

    order = sorted(range(len(blocks)), key=lambda k: rank(blocks[k]))
    if order == list(range(len(blocks))):
        return h
    out, prev = [], 0
    for slot, src_i in zip(blocks, order):
        out.append(seg[prev:slot[0]])
        out.append(blocks[src_i][2])
        prev = slot[1]
    out.append(seg[prev:])
    return h[:i] + ''.join(out) + h[j:]


def board_compact(h):
    """GỌN TOÀN BẢNG — chạy cuối pipeline, trước stamp (Tom 31/10)."""
    excluded = set()
    try:
        # load_zones() đã lọc khóa _ khỏi ZONES -> đọc thẳng zones.json
        # (cùng mẫu các hàm shark_focus/steel_net trong file này).
        _zp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "zones.json")
        excluded = set(json.load(open(_zp, encoding="utf-8"))
                       .get("_excluded", []))
    except Exception:
        excluded = set()
    for sec_id in re.findall(r'<div class="sec" id="([^"]+)"', h):
        h, _rm = dedup_alert_section(h, sec_id)
    h = _compact_chip_dedup(h, excluded)
    if excluded:
        h = _compact_excluded_rows(h, excluded)
    h = _compact_caps(h)
    h = _compact_ltalloc(h)
    h = _sort_ltalloc_actions(h)
    for sec_id in _SUMMARY_SECS:
        h = _compact_summary(h, sec_id)
    return h


def dash_top_merge(h):
    """🧭 BẢNG ĐIỀU KHIỂN ĐẦU BẢNG — Tom lệnh 3/11 (~12:47 PT, kèm ảnh chụp):
    4 dải rời ở đầu bảng (Bull vs Bear + Chế độ thị trường + VIX + Fear&Greed/
    Cổng săn) lặp số nhau, đọc rối — GOM THÀNH 1 KHỐI GỌN duy nhất, nhìn là
    focus. Hàm chạy CUỐI pipeline, ngay trước stamp_version ở MỌI mode: các
    writer cũ vẫn ghi 4 div riêng như thường (giữ nguyên logic/số liệu), hàm
    này trích số thật từ chính 4 div đó, XÓA cả 4, chèn 1 card #dashTop bọc
    marker DASHTOP-START/END ngay sau badge 'Cập nhật lần cuối'. Thiếu <2 div
    hoặc thiếu anchor -> giữ nguyên h (an toàn). Lần chạy sau: writer ghi lại
    4 div mới, merge xóa card cũ theo marker rồi dựng card mới — không chồng.
    """
    import html as _html

    def grab(pat):
        m = re.search(pat, h, re.S)
        return m.group(0) if m else ""

    def text_of(div):
        t = re.sub(r"<br\s*/?>", " | ", div)
        t = re.sub(r"<[^>]+>", " ", t)
        return " ".join(_html.unescape(t).split())

    bb = grab(r'<div class="bullbear" id="bullBear".*?</div>')
    rg = grab(r'<div class="regime" id="regimeStrip".*?</div>')
    vx = grab(r'<div class="note[^"]*" id="vix-gauge">.*?</div>')
    fg = grab(r'<div class="note[^"]*" id="fg-gauge">.*?</div>')
    if sum(bool(x) for x in (bb, rg, vx, fg)) < 2:
        return h
    if not re.search(r'<span class="updated">.*?</span>', h, re.S):
        return h

    # --- Bull vs Bear: verdict + phần số chỉ số ---
    bb_t = text_of(bb)
    bb_t = re.sub(r"^.*?BULL vs BEAR — ", "", bb_t)
    bb_t = re.sub(r" • \d{1,2}:\d{2} PT • .*$", "", bb_t)
    m = re.match(r"(\S+ [A-ZÀ-Ỹ ]+?) ((?:S&P|Nasdaq|Dow).*)", bb_t)
    bb_head, bb_rest = (m.group(1), m.group(2)) if m else (bb_t, "")

    # --- Chế độ: nhãn + dòng MA + ghi chú hành động ---
    rg_t = text_of(rg)
    m = re.search(r"CHẾ ĐỘ THỊ TRƯỜNG — (.+?) (SPY \$.*?) · theo ", rg_t)
    rg_label, rg_idx = (m.group(1), m.group(2)) if m else ("", "")
    rg_note = rg_t.split(" | ", 1)[1] if " | " in rg_t else ""
    accent = ("#dc2626" if "PHÒNG THỦ" in rg_label else
              "#16a34a" if "TẤN CÔNG" in rg_label else "#eab308")

    # --- VIX: số + nhãn ---
    vx_t = text_of(vx)
    m = re.search(r"VIX ([\d.]+) — (.+?) kiểm tra", vx_t)
    vx_s = f"VIX {m.group(1)} ({m.group(2)})" if m else ""

    # --- Fear & Greed: điểm + rating + cổng săn ---
    fg_t = text_of(fg)
    m = re.search(r"Fear & Greed ([\d.]+) — ([A-Z ]+)", fg_t)
    fg_s = f"F&G {m.group(1)} {m.group(2).strip()}" if m else ""
    gate = ("🦈 CỔNG SĂN MỞ" if "CỔNG SĂN MỞ" in fg_t else
            "🔒 CỔNG SĂN ĐÓNG" if "CỔNG SĂN ĐÓNG" in fg_t else "")

    E = _html.escape

    def _num(s):
        try:
            return float(str(s).replace(",", ""))
        except Exception:
            return None

    def _vn_pct(x):
        return f"{x:+.1f}".replace(".", ",") + "%"

    # ===== DASHTOP V3 — Tom lệnh thu gọn 3/11 ~14:15 PT: bản v2 chiếm nửa
    # màn hình phone, phải thu nhỏ lại một nửa. V3 gộp dòng: (1) dòng đầu =
    # CHẾ ĐỘ + VIX + F&G; (2) thanh F&G gradient mỏng; (3-4) SPY/QQQ đòi lại
    # MA20, thanh mỏng; (5) PHE % 1 dòng + thanh kéo co mỏng; (6) ghi chú
    # rút gọn 1 dòng + cổng săn. Bỏ: dòng futures, điểm phe thô, hero F&G
    # 2 dòng, đuôi dài "còn ... nữa mới tới MA20". Chữ giữ cỡ rõ nét.
    # Thiếu dữ liệu cốt lõi -> rơi về layout v1 ở ngay dưới.
    body = None
    try:
        _ms = re.search(r"SPY \$([\d,]+(?:\.\d+)?) · MA20 \$([\d,]+(?:\.\d+)?)", rg_t)
        _mq = re.search(r"QQQ \$([\d,]+(?:\.\d+)?) · MA20 \$([\d,]+(?:\.\d+)?)", rg_t)
        _mp = re.search(r"điểm phe ([+-]?[\d.]+)", bb_t)
        _mf = re.search(r"Fear & Greed ([\d.]+) — ([A-Z ]+)", fg_t)
        if _ms and _mq and _mp and _mf and rg_label:
            _spy = (_num(_ms.group(1)), _num(_ms.group(2)))
            _qqq = (_num(_mq.group(1)), _num(_mq.group(2)))
            _score = float(_mp.group(1))
            _fgv = float(_mf.group(1))
            _rating_vn = {"EXTREME FEAR": "CỰC KỲ SỢ HÃI", "FEAR": "SỢ HÃI",
                          "NEUTRAL": "TRUNG LẬP", "GREED": "THAM LAM",
                          "EXTREME GREED": "CỰC KỲ THAM LAM"}.get(
                              _mf.group(2).strip(), _mf.group(2).strip())
            _vx_html = ""
            if vx_s:
                _mvx = re.search(r"VIX ([\d.]+)", vx_s)
                _vxn = float(_mvx.group(1)) if _mvx else 0.0
                _vxc = ("#f87171" if _vxn >= 25 else "#fb923c" if _vxn >= 20
                        else "#eab308" if _vxn >= 15 else "#4ade80")
                _vx_html = f' · 🌡️ <b style="color:{_vxc}">{E(vx_s)}</b>'
            _prev = ""
            try:
                _fgp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "fear_greed.json")
                _stf = json.load(open(_fgp, encoding="utf-8"))
                if _stf.get("prev_close"):
                    _prev = f' · hôm qua {_stf["prev_close"]:.0f}'
            except Exception:
                pass
            _fgc = ("#f97316" if _fgv <= 25 else "#eab308" if _fgv <= 45 else
                    "#a3a380" if _fgv <= 55 else "#84cc16" if _fgv <= 75
                    else "#16a34a")

            def _ma20_row(tk, cm):
                close, ma20 = cm
                d = (close / ma20 - 1) * 100 if ma20 else 0.0
                if d >= 0:
                    fill, col, tail = 100, "#16a34a", "ĐÃ ĐÒI LẠI MA20 ✓"
                else:
                    fill = max(4, min(96, 100 + d * 8))
                    col = "#eab308" if d >= -3 else "#dc2626"
                    tail = f"{_vn_pct(d)} tới MA20"
                bar = ('<span class="dt-bar" style="display:inline-block;width:84px;height:8px;'
                       'background:rgba(148,163,184,.25);border-radius:4px;'
                       'overflow:hidden;vertical-align:middle;margin:0 6px">'
                       f'<span style="display:block;height:100%;width:{fill:.0f}%;'
                       f'background:{col}"></span></span>')
                return (f'<div class="dt-ma">{tk} <b>${close:,.0f}</b> → MA20 ${ma20:,.0f} '
                        f'{bar}<b class="dt-pct" style="color:{col}">{tail}</b></div>')

            _bull = max(3, min(97, round(50 + _score * 12)))
            _bear = 100 - _bull
            # Tom 3/11 ~14:28: bỏ dòng 📌 dài — trạng thái cổng mua MỞ/ĐÓNG
            # chuyển xuống dòng cuối của khối dạng badge highlight (xem footer).
            _rows3 = [
                (f'<div style="font-size:15px">🧭 <b>CHẾ ĐỘ — {E(rg_label)}</b>'
                 f'{_vx_html}<span class="dt-sep"> · </span><span class="dt-fg">😨 FEAR & GREED <b style="color:{_fgc};font-size:19px">'
                 f'{_fgv:.0f}</b> <b>{E(_rating_vn)}</b>'
                 f'<span style="color:#c9d6f2;font-size:12px;margin-left:6px">'
                 f'hôm nay {_fgv:.1f}{_prev}</span></span></div>'),
                ('<div style="position:relative;height:6px;border-radius:3px;'
                 'margin:4px 0 5px;background:linear-gradient(90deg,#dc2626,'
                 '#f97316,#eab308,#84cc16,#16a34a)">'
                 f'<div style="position:absolute;top:-2px;left:calc({_fgv:.0f}% - 2px);'
                 'width:4px;height:11px;background:#fff;border-radius:2px;'
                 'box-shadow:0 0 4px rgba(0,0,0,.7)"></div></div>'),
                _ma20_row("SPY", _spy),
                _ma20_row("QQQ", _qqq),
                (f'<div style="margin-top:4px">🐂 <b style="color:#4ade80">'
                 f'BULL {_bull}%</b> · 🐻 <b style="color:#f87171">BEAR '
                 f'{_bear}%</b></div>'),
                ('<div style="height:7px;border-radius:4px;overflow:hidden;'
                 'background:#dc2626;margin:3px 0 1px">'
                 f'<div style="height:100%;width:{_bull}%;background:#16a34a">'
                 '</div></div>'),
            ]
            body = "".join(_rows3)
    except Exception:
        body = None
    if body is None:
        rows = []
        if rg_label:
            idx = f' <span style="color:#9fb3d9">{E(rg_idx)}</span>' if rg_idx else ""
            rows.append(f"<div>🧭 <b>CHẾ ĐỘ — {E(rg_label)}</b>{idx}</div>")
        if bb_head:
            rest = f' <span style="color:#9fb3d9">{E(bb_rest)}</span>' if bb_rest else ""
            rows.append(f"<div>⚔️ <b>PHE — {E(bb_head)}</b>{rest}</div>")
        fear_bits = vx_s or ""
        if fg_s or fear_bits:
            fb = f' <span style="color:#9fb3d9">{E(fear_bits)}</span>' if fear_bits else ""
            rows.append(f"<div>😨 <b>SỢ HÃI — {E(fg_s)}</b>{fb}</div>")
        if not rows:
            return h
        body = "".join(rows)
    # Tom 3/11 ~14:28: highlight CỔNG MUA MỞ/ĐÓNG ở dòng cuối khối (thay dòng 📌)
    if "TẮT" in (rg_note or "") or "PHÒNG THỦ" in (rg_label or ""):
        _gate_badge = ('<span style="background:#7f1d1d;color:#fff;padding:1px 8px;'
                       'border-radius:6px;font-weight:800">⛔ KHÔNG MUA MÃ MỚI</span>'
                       '<span class="dt-note"> chờ SPY/QQQ đòi lại MA20</span>')
    elif "TRUNG LẬP" in (rg_label or ""):
        _gate_badge = ('<span style="background:#92400e;color:#fff;padding:1px 8px;'
                       'border-radius:6px;font-weight:800">⚠️ MUA MỚI: HẠN CHẾ</span>'
                       '<span class="dt-note"> chờ xác nhận đảo chiều</span>')
    elif rg_label:
        _gate_badge = ('<span style="background:#14532d;color:#fff;padding:1px 8px;'
                       'border-radius:6px;font-weight:800">✅ MUA MỚI: MỞ</span>'
                       '<span class="dt-note"> mua theo vùng duyệt</span>')
    else:
        _gate_badge = "Bảng điều khiển đầu bảng"
    # Tom 3/11 18:59 PT: monitor OK nhung phone bi rot dong/che khu ->
    # media-query chi cho man hep (<=640px), desktop giu nguyen 100%.
    # Style nam TRONG card (rebuild moi render) nen khong ton tai ban sao lac.
    _dt_css = ('<style>@media (max-width:640px){'
               '#dashTop .dt-sep{display:none}'
               '#dashTop .dt-fg{display:block;margin-top:1px}'
               '#dashTop .dt-bar{width:50px!important}'
               '#dashTop .dt-pct{white-space:nowrap}'
               '#dashTop .dt-note{white-space:nowrap}'
               '#dashTop .dt-upd{white-space:nowrap}'
               '#dashTop .dt-foot{padding-right:44px}'
               '}</style>')
    card = ("<!-- DASHTOP-START -->\n"
            f'<div class="dashtop" id="dashTop" style="border:2px solid #d4af37;'
            f"border-left:7px solid {accent};border-radius:12px;padding:8px 11px;"
            f'margin:6px 0 2px;background:rgba(212,175,55,.06);font-size:13.5px;'
            f'line-height:1.45">' + _dt_css + body +
            f'<div class="dt-legend" style="font-size:12px;margin-top:3px">'
            f'🟨 MÃ GỐC · 🟦 XE 2x · 🌙 XE CHẠY ĐÊM 24H</div>' +
            f'<div class="dt-foot" style="font-size:12px;margin-top:4px">{_gate_badge}'
            f'<span class="dt-upd" style="opacity:.65"> · cập nhật {now_pt().strftime("%H:%M")} PT</span>'
            f"</div></div>\n<!-- DASHTOP-END -->\n")

    for pat in (r'<div class="bullbear" id="bullBear".*?</div>\n?',
                r'<div class="regime" id="regimeStrip".*?</div>\n?',
                r'<div class="note[^"]*" id="vix-gauge">.*?</div>\n?',
                r'<div class="note[^"]*" id="fg-gauge">.*?</div>\n?',
                r"<!-- DASHTOP-START -->.*?<!-- DASHTOP-END -->\n?"):
        h = re.sub(pat, "", h, flags=re.S)
    h2, c = re.subn(r'(<span class="updated">.*?</span>)', r"\g<1>\n" + card,
                    h, count=1, flags=re.S)
    return h2 if c else h


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
    """CỔNG DUY NHẤT (phá trận 4/11 — lệnh Tom): hàm này KHÔNG tự git
    pull/commit/push nữa. Mọi lượt đẩy giao deploy.py: nó gom MỌI file đổi
    (web + state, theo danh sách FILES của deploy.py) thành đúng 1 commit
    qua Git Data API rồi verify cặp live. Giữ nguyên hợp đồng trả về cho
    caller: "pushed" / "nothing-to-commit" / "push-failed" — và KHÔNG bao
    giờ trả "pushed" giả khi chưa thấy bằng chứng trong output của cổng
    (bài học 16/10: push chưa lên mà in "pushed"). deploy.py đang bận
    (flock — "lượt khác đang chạy") thì chờ 25 giây thử lại, tối đa 6 lần;
    hết lượt hoặc cổng lỗi thì thành thật "push-failed" để lượt sau xử lý.
    Tham số msg giữ cho tương thích chữ ký, cổng tự ghi log commit."""
    for attempt in range(6):
        try:
            r = subprocess.run([sys.executable,
                                os.path.join(repo_dir, "deploy.py")],
                               cwd=repo_dir, capture_output=True, text=True,
                               timeout=600)
        except Exception:
            return "push-failed"
        out = (r.stdout or "") + (r.stderr or "")
        if "lượt khác đang chạy" in out:
            if attempt < 5:
                time.sleep(25)
                continue
            return "push-failed"
        if "0 commit" in out:
            return "nothing-to-commit"
        if "VERIFIED" in out or "1 commit" in out:
            return "pushed"
        return "push-failed"
    return "push-failed"


def close_headers(h):
    """Chốt phiên 13:05 — đổi header intraday thành đóng cửa cho đúng ngày."""
    today = now_pt().strftime("%d/%m")
    h = h.replace("GIỮA PHIÊN", "ĐÓNG CỬA")
    h = h.replace("giữa phiên", "chốt phiên")
    h = re.sub(r"\(14/10 — giữa phiên sáng\)", f"({today} — chốt phiên)", h)
    h = re.sub(r"\(\d{2}/\d{2} — giữa phiên sáng\)", f"({today} — chốt phiên)", h)
    return h


LEV_MAP = {"SOXL": "SOXL", "SNXX": "SNDK", "MUU": "MU", "CONL": "COIN",
           "CIEG": "CIEN", "DRAM": "DRAM", "RAM": "DRAM", "NVDL": "NVDA",
           "GGLL": "GOOGL", "AVGX": "AVGO", "TQQQ": "QQQ", "AMZU": "AMZN",
           "TSLL": "TSLA", "TNA": "IWM", "SPCH": "SPCX", "VSTL": "VST",
           "METU": "META", "SKUU": "SKHY", "AMDL": "AMD", "ORCU": "ORCL",
           "MSFU": "MSFT", "TSMX": "TSM", "RKLX": "RKLB", "CRWC": "CRWD",
           "CRMG": "CRM",
           "ASTX": "ASTS", "DLLL": "DELL", "AAPU": "AAPL", "PLTU": "PLTR",
           "PLTZ": "PLTR",
           "INTW": "INTC", "STXU": "STX", "STXX": "STX", "STXL": "STX", "CEGX": "CEG", "IONX": "IONQ",
           "AVXX": "AVAV", "KTUP": "KTOS", "OKLL": "OKLO", "OKTG": "OKTA",
           "RDDC": "RDDT", "NOWL": "NOW", "CRDU": "CRDO", "ROBN": "HOOD", "LRCU": "LRCX",
           "COHX": "COHR",
           "MULL": "MU",
           "NBIL": "NBIS",
           "TSLG": "TSLA",
           "TSLR": "TSLA",
           "SMCX": "SMCI",
           "NVDX": "NVDA",
           "NVDG": "NVDA",
           "MVLL": "MRVL",
           "CONY": "COIN",
           "SNDU": "SNDK",
           "SNDG": "SNDK",
           "AAOX": "AAOI",
           "KLAG": "KLAC",
           "LITX": "LITE",
           "CSCL": "CSCO",
           "VRTL": "VRT",
           "ANEL": "ANET",
           "LABX": "ALAB",
           "RTXG": "RTX",
           "LMTL": "LMT",
           "GLWG": "GLW",
           "WDCX": "WDC"}


# --- XE X2 COMPANION (Tom 30/10 12:48): mẫu mục ĐÒN BẨY áp cho các mục khác.
# Mỗi mã gốc có 1 xe x2 tốt nhất (thanh khoản cao nhất); alert/row của mã đó
# được gắn thêm 1 dòng companion ngay dưới headline: giá xe realtime +
# trạng thái vùng mã gốc (cùng logic/màu/chớp như lchip ở sec-leverage) +
# vùng dịch theo tỷ lệ giá realtime (công thức levmap đang chạy ở sec-leverage).
# TQQQ/TNA là xe chỉ số, KHÔNG gắn companion.
# CỐ Ý KHÔNG gắn: sec-ltalloc + sec-etf/sec-lt9/sec-etf16 (tiền dài hạn
# 10–20 năm, xe x2 ngược triết lý) · sec-leverage (đã đủ 3 lớp) · sec-news
# và các mục tin tức/chỉ nam.
# MỞ RỘNG 4/11 (Tom chốt 20:26 — LUẬT GẮN CẶP TOÀN BẢNG): MỌI mã trên bảng
# đều gắn xe — KỂ CẢ MÃ WATCH-ONLY — nơi có xe thật đã kiểm chứng Yahoo.
# Mẫu thống nhất: nhãn mã gốc VÀNG (gradient #ffe066→#f5c518 như .tk của
# sec-tinhhoa) · nhãn xe XANH DA TRỜI #7dd3fc (như sec-leverage) · 🌙 24H
# chỉ khi xe nằm trong LEV_24H (snapshot Robinhood đã kiểm chứng).
COMP_MAP = {
    "NVDA": "NVDL", "GOOGL": "GGLL", "AVGO": "AVGX", "TSM": "TSMX",
    "MSFT": "MSFU", "ORCL": "ORCU", "AMD": "AMDL", "META": "METU",
    "AMZN": "AMZU", "TSLA": "TSLL", "SNDK": "SNXX", "MU": "MUU",
    "COIN": "CONL", "CIEN": "CIEG", "RKLB": "RKLX", "ASTS": "ASTX",
    "IONQ": "IONX", "AVAV": "AVXX", "KTOS": "KTUP", "CEG": "CEGX",
    "VST": "VSTL", "SPCX": "SPCH", "DELL": "DLLL", "AAPL": "AAPU",
    "PLTR": "PLTU", "INTC": "INTW", "STX": "STXU", "SKHY": "SKUU",
    "OKLO": "OKLL", "OKTA": "OKTG", "RDDT": "RDDC", "NOW": "NOWL",
    "CRWD": "CRWC", "IREN": "IRE", "DRAM": "RAM", "CRDO": "CRDU", "HOOD": "ROBN", "LRCX": "LRCU",
    "KLAC": "KLAG", "LITE": "LITX", "CSCO": "CSCL", "VRT": "VRTL",
    "ANET": "ANEL", "ALAB": "LABX", "CRM": "CRMG",
    "RTX": "RTXG", "LMT": "LMTL", "GLW": "GLWG", "WDC": "WDCX",
    # bổ sung 4/11 (mở rộng toàn bảng — mọi xe đã kiểm chứng Yahoo quote
    # 4/11: tên quỹ 2x Long đúng mã gốc + giá thật; vol TB 5 phiên 4/11
    # ghi ở COMP_WARN nếu mỏng)
    "COHR": "COHX", "NBIS": "NBIL", "SMCI": "SMCX", "MRVL": "MVLL",
    "AAOI": "AAOX", "MSTR": "MSTX", "BABA": "BABX", "NFLX": "NFXL",
    "GLD": "UGL", "SLV": "AGQ", "BA": "BOEU", "BMNR": "BMNU",
    "QCOM": "QCMU", "ARM": "ARMG", "APP": "APPX",
}
COMP_WARN = {  # tem cảnh báo tĩnh, số vol đã verify 30/10
    "CRWC": "⚠️ vol cực mỏng ~5,8k cp/ngày — gần như không giao dịch được",
    "VSTL": "⚠️ vol mỏng ~91k",
    "AVXX": "⚠️ vol mỏng ~39k",
    "RDDC": "⚠️ vol mỏng ~34k",
    "AMDL": "🚨 AMD earnings 3/11 — cấm ôm xe qua số",
    "LRCU": "⚠️ vol vừa ~140–200k cp/ngày, AUM ~$39tr — chia lệnh nhỏ, tránh giờ mỏng",
    "COHX": "🚨 COHR earnings 4/11 — cấm ôm xe qua số",
    "SMCX": "🚨 SMCI earnings CHIỀU NAY 3/11 — cấm ôm xe qua số",
    "KLAG": "⚠️ vol mỏng ~22k cp/ngày (3/11), AUM ~$11tr — chia lệnh nhỏ, tránh giờ mỏng",
    "LITX": "🚨 LITE earnings 5/11 AMC — cấm ôm xe qua số",
    "CSCL": "⚠️ vol cực mỏng ~6–11k cp/ngày, AUM ~$10tr — gần như không giao dịch được, chia lệnh nhỏ",
    "ANEL": "🚨 ANET earnings TỐI NAY 3/11 AMC — cấm ôm xe qua số · ⚠️ vol vừa ~61k cp/ngày (3/11) — chia lệnh nhỏ",
    "LABX": "🚨 ALAB earnings TỐI NAY 3/11 AMC — cấm ôm xe qua số",
    "RTXG": "⚠️ vol cực mỏng ~7,5k cp/ngày (3/11) — gần như không giao dịch được, chia lệnh nhỏ",
    "LMTL": "⚠️ vol mỏng ~10–31k cp/ngày, AUM ~$8tr — chia lệnh nhỏ, tránh giờ mỏng",
    "STXX": "⚠️ vol vừa ~100–160k cp/ngày, phí 1,49%/năm — chia lệnh nhỏ, tránh giờ mỏng",
    "CONY": "💸 KHÔNG phải xe x2 — quỹ YieldMax thu nhập quyền chọn: phí ~0,99%/năm · cổ tức TUẦN (kỳ 29/10: $0,181/cp), phần lớn là hoàn vốn — NAV mòn dần theo thời gian",
    "QCMU": "⚠️ vol vừa ~127k cp/ngày (TB 5 phiên, 4/11) — chia lệnh nhỏ, tránh giờ mỏng",
    "BOEU": "⚠️ vol mỏng ~99k cp/ngày (TB 5 phiên, 4/11) — chia lệnh nhỏ, tránh giờ mỏng",
}
COMP_SECTIONS = ("sec-daytrade", "sec-swing", "sec-so2", "sec-so3",
                 "sec-special", "sec-urgent",
                 # mở rộng 4/11 theo LUẬT GẮN CẶP TOÀN BẢNG
                 "sec-topfocus", "sec-tickets", "sec-watch", "sec-steelnet",
                 "sec-tinhhoa", "sec-hot", "sec-live")
_COMP_STATE_LAB = {"IN": "🟢 TRONG VÙNG", "BELOW": "🟡 DƯỚI VÙNG",
                   "ABOVE": "⚪ TRÊN VÙNG", "BROKEN": "🔴 GÃY VÙNG"}


def _levcomp_balanced_end(h, start):
    """Index ngay sau thẻ </div> cân bằng với <div mở tại start; None nếu lệch."""
    depth, j = 0, start
    while True:
        o = h.find('<div', j)
        c = h.find('</div>', j)
        if c == -1:
            return None
        if o != -1 and o < c:
            depth += 1
            j = o + 4
        else:
            depth -= 1
            j = c + 6
            if depth == 0:
                return j


def _levcomp_strip(h):
    """Xóa trọn mọi dòng .levcomp cũ + hàng <tr class="levcomp-row"> cũ.
    Quét cân bằng tới thẻ đóng của element — KHÔNG regex non-greedy dừng
    giữa chừng (bài học bug nhân đôi levtag 30/10)."""
    while True:
        m = re.search(r'<tr class="levcomp-row">', h)
        if not m:
            break
        e = h.find('</tr>', m.start())
        if e == -1:
            break
        h = h[:m.start()] + h[e + 5:]
    out, i = [], 0
    pat = re.compile(r'<div class="levcomp[^>]*>')
    while True:
        m = pat.search(h, i)
        if not m:
            out.append(h[i:])
            break
        start = m.start()
        end = _levcomp_balanced_end(h, start)
        if end is None:
            out.append(h[i:])
            return ''.join(out)
        out.append(h[i:start])
        i = end
    return ''.join(out)


def _comp_line(u, v, px, zones, suffix=""):
    """(dòng companion, chớp?) cho mã gốc u / xe v; (None, False) nếu thiếu giá.
    BROKEN → chỉ ghi 'MÃ GỐC ĐÃ GÃY CẮT — số dịch chỉ tham chiếu', TUYỆT ĐỐI
    không ghi chữ MUA. Vùng TẠM TREO → không dịch số, như levmap.
    Mẫu 4/11: nhãn mã gốc VÀNG + nhãn xe XANH DA TRỜI (chip data-px/data-pct
    để streamer/poller cập nhật realtime như sec-leverage) + 🌙 24H khi xe
    có trong LEV_24H. suffix: câu đuôi riêng theo mục (vd cảnh báo earnings
    ở sec-tinhhoa)."""
    if v not in px or u not in px:
        return None, False
    vpx, vpct = px[v]
    upx = px[u][0]
    if not upx:
        return None, False
    gold = ('<span style="background:linear-gradient(180deg,#ffe066,#f5c518);'
            'color:#111;font-weight:900;padding:1px 7px;border-radius:5px">'
            f"{u}</span>")
    sky = ('<span style="background:#7dd3fc;color:#131313;font-weight:900;'
           'padding:1px 7px;border-radius:5px">'
           f'<span data-px="{v}">{v} {_lt_money(vpx)}</span></span>')
    moon = " · 🌙 24H" if v in LEV_24H else ""
    head = (f"{gold} ↳ XE X2 · SỔ 3 CHỈ DAYTRADE: {sky} "
            f'<span data-pct="{v}">({fmt_pct_vn(vpct)})</span>{moon}')
    warn = COMP_WARN.get(v)
    if warn:
        head += f" · {warn}"
    z = zones.get(u)
    if not z or not z.get("entry"):
        return head + f" · {u} chưa có vùng — xe chạy theo đà" + suffix, False
    note = (z.get("note") or "").upper()
    if "TAM VO HIEU" in note or "TEMP-INVALID" in note:
        return head + f" · {u} VÙNG TẠM TREO — chưa dịch giá xe" + suffix, False
    try:
        lo, hi = z["entry"]
        st = _zone_state_of(z, upx)
    except Exception:
        return None, False
    r = vpx / upx
    body = (f"MÃ GỐC ĐÃ GÃY CẮT — số dịch chỉ tham chiếu · "
            f"Vùng dịch {_lt_money(lo * r)}–{_lt_money(hi * r)}"
            if st == "BROKEN" else
            f"{_COMP_STATE_LAB[st]} · "
            f"Vùng dịch {_lt_money(lo * r)}–{_lt_money(hi * r)}")
    tps = z.get("targets") or z.get("tp") or []
    if isinstance(tps, (int, float)):
        tps = [tps]
    if tps:
        body += " · Chốt " + "→".join(_lt_money(t * r) for t in tps[:2])
    if z.get("cut"):
        body += f" · Cắt xe ≈ <{_lt_money(z['cut'] * r)}"
    return head + " · " + body + suffix, (st == "IN")


def _comp_alert_ticker(full):
    """Mã chính của 1 alert: chip/ticker attr định danh trước (không ăn theo
    mã nhắc trong bold — tránh gắn nhầm companion cho alert của mã khác)."""
    for attr in ("data-achip", "data-ticker", "data-fchip"):
        m = re.search(attr + r'="([A-Z]{2,6})"', full)
        if m:
            return m.group(1) if m.group(1) in COMP_MAP else None
    m = re.search(r"<b>(.*?)</b>", full, re.S)
    if m:
        txt = re.sub(r"<[^>]+>", " ", m.group(1))
        for tok in re.findall(r"\b([A-Z]{2,6})\b", txt):
            if tok in COMP_MAP:
                return tok
    return None


def _comp_insert_alert(full, u, line, flash):
    """Chèn dòng companion NGAY DƯỚI headline của 1 alert; full nguyên trạng
    nếu không xác định được headline (không đếm)."""
    cls = "levcomp levflash" if flash else "levcomp"
    div = f'<div class="{cls}">{line}</div>'
    for bm in re.finditer(r"<b>(.*?)</b>", full, re.S):
        txt = re.sub(r"<[^>]+>", " ", bm.group(1))
        if re.search(r"\b" + u + r"\b", txt):
            return full[:bm.end()] + div + full[bm.end():]
    om = re.match(r'<div class="alert[^>]*>(<div class="zchip[^>]*>)', full)
    if om:
        end = _levcomp_balanced_end(full, om.start(1))
        if end is not None:
            return full[:end] + div + full[end:]
    cm = re.search(r"</b>", full[:800])
    if cm:
        return full[:cm.end()] + div + full[cm.end():]
    return full


def update_companion_lines(h, prices):
    """Gắn dòng companion xe x2 vào các mục đích (COMP_SECTIONS + khối 👑
    FOCUS). Strip trước rồi gắn lại nên idempotent; fail-soft giữ nguyên bảng.
    Trả về (h, tổng_số_dòng, {mục: số_dòng})."""
    try:
        h = _levcomp_strip(h)
        if ".levcomp{" not in h:
            css = (".levcomp{margin-top:6px;border:1px dashed #f59e0b;"
                   "border-radius:8px;padding:6px 9px;font-size:14px;"
                   "background:rgba(245,158,11,.08)}")
            i = h.find("</style>")
            if i > 0:
                h = h[:i] + css + h[i:]
        zones, _excl = _zones_local()
        spans = {}
        for sec in COMP_SECTIONS:
            m = re.search(r'<div class="sec" id="' + sec + r'"[^>]*>', h)
            if not m:
                continue
            start = m.start()
            nxt = h.find('<div class="sec"', start + 10)
            spans[sec] = (start, nxt if nxt != -1 else len(h))
        fi = h.find("FOCUS — CÁ MẬP")
        if fi != -1:
            fs = h.rfind('<div class="sharkfocus"', 0, fi)
            fe_m = h.find("<!-- /SHARKFOCUS-AUTO -->", fi)
            if fs != -1 and fe_m != -1:
                fe = h.find("</div>", fe_m)
                if fe != -1:
                    spans["FOCUS"] = (fs, fe + 6)
        if not spans:
            return h, 0, {}
        # --- giá cần fetch: xe + mã gốc của mọi mã ứng viên trong các mục đích
        cand = set()
        for s0, s1 in spans.values():
            seg = h[s0:s1]
            for attr in ("data-achip", "data-ticker", "data-fchip"):
                cand.update(re.findall(attr + r'="([A-Z]{2,6})"', seg))
            cand.update(re.findall(r"<tr><td><b>([A-Z]{2,6})</b></td>", seg))
            # mẫu riêng từng mục (4/11): lưới thép dùng data-t trên
            # zchip-row · earnings dùng <span class='tk'> · live strip
            # dùng <div class="lt">
            cand.update(re.findall(r'data-t="([A-Z]{2,6})"', seg))
            cand.update(re.findall(r"class=['\"]tk['\"]>([A-Z0-9.]{1,8})<", seg))
            cand.update(re.findall(r'<div class="lt">([A-Z]{2,6})', seg))
            for bm in re.finditer(r"<b>(.*?)</b>", seg, re.S):
                txt = re.sub(r"<[^>]+>", " ", bm.group(1))
                cand.update(t for t in re.findall(r"\b([A-Z]{2,6})\b", txt)
                            if t in COMP_MAP)
        cand = {t for t in cand if t in COMP_MAP}
        px = dict(prices)
        need = set()
        for u in cand:
            need.add(u)
            need.add(COMP_MAP[u])
        missing = [t for t in sorted(need) if t not in px]
        if missing:
            got, _l, _h52 = fetch_prices(missing)
            px.update(got)
        counts = {}
        # --- xử lý từng mục từ CUỐI file về đầu để span trước không lệch
        for name in sorted(spans, key=lambda k: -spans[k][0]):
            s0, s1 = spans[name]
            seg = h[s0:s1]
            seen = set()
            n0 = 0

            def _mk_line(u):
                line, flash = _comp_line(u, COMP_MAP[u], px, zones)
                return line, flash

            if name in ("sec-so2", "sec-so3"):
                def _row_rep(m):
                    nonlocal n0
                    u = m.group(1)
                    if u not in COMP_MAP or u in seen:
                        return m.group(0)
                    line, flash = _mk_line(u)
                    if not line:
                        return m.group(0)
                    seen.add(u)
                    n0 += 1
                    cls = "levcomp levflash" if flash else "levcomp"
                    return (m.group(0) +
                            f'<tr class="levcomp-row"><td colspan="4">'
                            f'<div class="{cls}">{line}</div></td></tr>')
                seg = re.sub(r"<tr><td><b>([A-Z]{2,6})</b></td>.*?</tr>",
                             _row_rep, seg, flags=re.S)
            elif name == "sec-steelnet":
                # lưới thép có 2 dạng hàng: <tr class="zchip-row" data-t>
                # (chip tóm tắt) + <tr><td><b> (bảng chi tiết) — gắn 1 dòng
                # companion cho mỗi mã (seen chặn trùng), colspan=5.
                def _sn_rep(m):
                    nonlocal n0
                    u = m.group(1)
                    if u not in COMP_MAP or u in seen:
                        return m.group(0)
                    line, flash = _mk_line(u)
                    if not line:
                        return m.group(0)
                    seen.add(u)
                    n0 += 1
                    cls = "levcomp levflash" if flash else "levcomp"
                    return (m.group(0) +
                            f'<tr class="levcomp-row"><td colspan="5">'
                            f'<div class="{cls}">{line}</div></td></tr>')
                seg = re.sub(r'<tr class="zchip-row" data-t="([A-Z]{2,6})">'
                             r".*?</tr>", _sn_rep, seg, flags=re.S)
                seg = re.sub(r"<tr><td><b>([A-Z]{2,6})</b></td>.*?</tr>",
                             _sn_rep, seg, flags=re.S)
            elif name == "sec-tinhhoa":
                # entry earnings: <div class='alert' ...> (NHÁY ĐƠN — khác
                # mọi mục khác) + mã ở <span class='tk'>; dòng xe chèn ngay
                # sau <br> đầu tiên của entry + LUẬT TOM: kèm cảnh báo cấm
                # ôm xe qua đêm ra số.
                _earn_suffix = " · ⚠️ XE X2: CẤM ÔM QUA ĐÊM RA SỐ"
                out, i = [], 0
                pat = re.compile(r"<div class='alert[^>]*>")
                while True:
                    m = pat.search(seg, i)
                    if not m:
                        out.append(seg[i:])
                        break
                    e_end = _levcomp_balanced_end(seg, m.start())
                    if e_end is None:
                        out.append(seg[i:])
                        break
                    entry = seg[m.start():e_end]
                    out.append(seg[i:m.start()])
                    tm = re.search(r"class='tk'>([A-Z0-9.]{1,8})<", entry)
                    u = tm.group(1) if tm else None
                    if u and u in COMP_MAP and u not in seen:
                        line, flash = _comp_line(u, COMP_MAP[u], px, zones,
                                                 suffix=_earn_suffix)
                        if line:
                            bm = re.search(r"<br\s*/?>", entry)
                            pos = bm.end() if bm else tm.end()
                            cls = ("levcomp levflash" if flash
                                   else "levcomp")
                            entry = (entry[:pos] +
                                     f'<div class="{cls}">{line}</div>' +
                                     entry[pos:])
                            seen.add(u)
                            n0 += 1
                    out.append(entry)
                    i = e_end
                seg = ''.join(out)
            elif name == "sec-live":
                # live strip: mỗi mã 1 .livechip trong #liveStrip (JS tự
                # cập nhật giá trong strip) — gắn các dòng companion NGAY
                # SAU strip, vẫn trong sec-live, không đụng vào strip.
                sm = re.search(r'<div id="liveStrip"[^>]*>', seg)
                if sm:
                    send = _levcomp_balanced_end(seg, sm.start())
                    if send is not None:
                        strip = seg[sm.start():send]
                        lines = []
                        for u in re.findall(r'<div class="lt">([A-Z]{2,6})',
                                            strip):
                            if u in COMP_MAP and u not in seen:
                                line, flash = _mk_line(u)
                                if line:
                                    seen.add(u)
                                    n0 += 1
                                    cls = ("levcomp levflash" if flash
                                           else "levcomp")
                                    lines.append(
                                        f'<div class="{cls}">{line}</div>')
                        if lines:
                            seg = (seg[:send] + ''.join(lines) +
                                   seg[send:])
            elif name == "FOCUS":
                fpat = re.compile(
                    r'<div style="border-top:1px solid rgba\(212,175,55,.45\)'
                    r'[^>]*>')
                out, i = [], 0
                while True:
                    fm = fpat.search(seg, i)
                    if not fm:
                        out.append(seg[i:])
                        break
                    fend = _levcomp_balanced_end(seg, fm.start())
                    if fend is None:
                        out.append(seg[i:])
                        break
                    entry = seg[fm.start():fend]
                    out.append(seg[i:fm.start()])
                    u = _comp_alert_ticker(entry)
                    if u and u not in seen:
                        line, flash = _mk_line(u)
                        if line:
                            hm = re.search(
                                r"<b>" + u + r"</b>.*?<br>", entry, re.S)
                            if hm:
                                cls = ("levcomp levflash" if flash
                                       else "levcomp")
                                entry = (entry[:hm.end()] +
                                         f'<div class="{cls}">{line}</div>' +
                                         entry[hm.end():])
                                seen.add(u)
                                n0 += 1
                    out.append(entry)
                    i = fend
                seg = ''.join(out)
            else:
                def _alert_fn(full, inner):
                    nonlocal n0
                    u = _comp_alert_ticker(full)
                    if not u or u in seen:
                        return full
                    line, flash = _mk_line(u)
                    if not line:
                        return full
                    new = _comp_insert_alert(full, u, line, flash)
                    if new != full:
                        seen.add(u)
                        n0 += 1
                    return new
                seg = sub_balanced_alerts(seg, _alert_fn)
            h = h[:s0] + seg + h[s1:]
            if n0:
                counts[name] = n0
        return h, sum(counts.values()), counts
    except Exception as e:
        print(f"  ! companion lines: {e}", file=sys.stderr)
        return h, 0, {}


def update_leverage_signals(h, prices):
    """Mục ĐÒN BẨY (Tom duyệt 30/10 12:27): thanh TÍN HIỆU NEO (data-lchip)
    + giá xe + khung chớp levflash TỰ CHẠY mỗi lượt refresh theo GIÁ ETF
    REALTIME và trạng thái vùng của mã gốc. Fail-soft: lỗi thì giữ nguyên."""
    try:
        if ".levmap{" not in h:
            css = (".levmap{margin-top:6px;border:1px dashed #38bdf8;"
                   "border-radius:8px;padding:6px 9px;font-size:14px;"
                   "background:rgba(56,189,248,.08)}")
            i = h.find("</style>")
            if i > 0:
                h = h[:i] + css + h[i:]
        m = re.search(r'<div class="sec" id="sec-leverage"[^>]*>', h)
        if not m:
            return h, 0
        start = m.start()
        nxt = h.find('<div class="sec"', start + 10)
        end = nxt if nxt != -1 else len(h)
        seg = h[start:end]
        vehs = re.findall(r'data-lchip="([A-Z]+)"', seg)
        if not vehs:
            return h, 0
        zones, _excl = _zones_local()
        need = set()
        for v in vehs:
            need.add(v)
            u = LEV_MAP.get(v)
            if u:
                need.add(u)
        px = dict(prices)
        missing = [t for t in need if t not in px]
        if missing:
            got, _l, _h52 = fetch_prices(sorted(missing))
            px.update(got)
        n = 0
        for v in vehs:
            u = LEV_MAP.get(v)
            if not u or v not in px:
                continue
            vpx, vpct = px[v]
            # --- thanh tín hiệu neo: giá ETF realtime đứng đầu ---
            chip_pat = re.compile(r'<div class="zchip [^"]*" data-lchip="'
                                  + v + r'">.*?</div>', re.S)
            cm = chip_pat.search(seg)
            if cm:
                head = (f"{v} {_lt_money(vpx)} ({fmt_pct_vn(vpct)})"
                        f" · NEO ")
                if u in zones and zones[u].get("entry") and u in px:
                    upx = px[u][0]
                    st = _zone_state_of(zones[u], upx)
                    cls, txt = _zone_chip_text(u, upx, st, zones[u])
                elif u in px:
                    cls = "zchip-n"
                    txt = (f"{u} · ⚪ CHƯA CÓ VÙNG · "
                           f"Giá gốc {_lt_money(px[u][0])}")
                else:
                    cls, txt = "zchip-n", f"{u} · ⚪ CHƯA CÓ GIÁ GỐC"
                new_chip = (f'<div class="zchip {cls}" data-lchip="{v}">'
                            + head + txt + "</div>")
                seg = seg[:cm.start()] + new_chip + seg[cm.end():]
                n += 1
            # --- khung chớp + giá xe trong thân alert ---
            in_zone = (u in zones and zones[u].get("entry") and u in px
                       and _zone_state_of(zones[u], px[u][0]) == "IN")
            apat = re.compile(
                r'<div class="alert(?: levflash)?"><b>'
                r'((?:⚡|⭐|🚨) ' + v + r' —.*?)</b>', re.S)
            am = apat.search(seg)
            if am:
                cls_alert = "alert levflash" if in_zone else "alert"
                tag = ('<span class="levtag">🔥 ĐANG LỌT VÙNG</span>'
                       if in_zone else "")
                clean = re.sub(r'<span class="levtag">.*?</span>', "",
                               am.group(1))
                new_head = (f'<div class="{cls_alert}"><b>'
                            + clean + tag + "</b>")
                seg = seg[:am.start()] + new_head + seg[am.end():]
                # giá xe realtime trong thân alert (sau headline)
                tail = seg[am.start() + len(new_head):
                           am.start() + len(new_head) + 700]
                pm = re.search(r'~\$[\d.,]+ \([−+\-]?[\d.,]+%\)', tail)
                if pm:
                    newp = (f"~{_lt_money(vpx)} ({fmt_pct_vn(vpct)})")
                    seg = (seg[:am.start() + len(new_head) + pm.start()]
                           + newp
                           + seg[am.start() + len(new_head) + pm.end():])
                # --- DỊCH VÙNG GỐC SANG GIÁ ETF (Tom 12:33 — 1 công 2 việc:
                # trên mã gốc, dưới mã ETF, vùng mua bán GIỐNG NHAU theo
                # tỷ lệ giá realtime, cùng 1 tín hiệu chớp) ---
                map_txt = None
                if u in zones and zones[u].get("entry") and u in px:
                    z = zones[u]
                    note = (z.get("note") or "").upper()
                    if "TAM VO HIEU" in note or "TEMP-INVALID" in note:
                        map_txt = (f"📐 {v} THEO VÙNG {u}: vùng gốc đang "
                                   f"TẠM TREO — chưa dịch giá ETF")
                    else:
                        r = vpx / px[u][0]
                        lo, hi = z["entry"]
                        tps = z.get("targets") or z.get("tp") or []
                        if isinstance(tps, (int, float)):
                            tps = [tps]
                        st_u = _zone_state_of(z, px[u][0])
                        lab = (f"📐 {v} THEO VÙNG {u} — ĐÃ GÃY CẮT, "
                               f"số dịch chỉ tham chiếu: "
                               if st_u == "BROKEN" else
                               f"📐 {v} MUA theo vùng {u}: ")
                        parts = [lab
                                 + f"{_lt_money(lo * r)}–{_lt_money(hi * r)}"]
                        if tps:
                            parts.append("Chốt " + "→".join(
                                _lt_money(t * r) for t in tps[:2]))
                        if z.get("cut"):
                            parts.append(
                                f"Cắt ETF ≈ <{_lt_money(z['cut'] * r)} "
                                f"(khi {u} thủng {_lt_money(z['cut'])})")
                        map_txt = " · ".join(parts)
                elif u in px:
                    map_txt = (f"📐 {v}: {u} chưa có vùng — chưa dịch được "
                               f"giá ETF, chỉ canh theo đà")
                if map_txt:
                    lev = f'<div class="levmap">{map_txt}</div>'
                    lpat = re.compile(r'<div class="levmap">.*?</div>',
                                      re.S)
                    astart = am.start()
                    aend = seg.find("</div>", astart)
                    if aend != -1:
                        lm = lpat.search(seg, astart, aend + 6)
                        if lm:
                            seg = seg[:lm.start()] + lev + seg[lm.end():]
                        else:
                            seg = seg[:aend] + lev + seg[aend:]
        return h[:start] + seg + h[end:], n
    except Exception as e:
        print(f"  ! leverage signals: {e}", file=sys.stderr)
        return h, 0


# --- CẶP MÃ GỐC + XE x2 (Tom 02/11: mục ĐÒN BẨY khó xem — đổi sang khối cặp:
# mã gốc TRÊN, xe x2 DƯỚI, mỗi hàng đủ VÙNG MUA · CẮT · TARGET; số xe dịch theo
# tỷ lệ giá realtime, tự tính lại mỗi lượt refresh; giá trong khối tự cập nhật
# 30 giây qua prices.json nhờ data-px/data-pct). Thay vai trò của chip strip.
LEV_X3 = {"SOXL", "TQQQ", "TNA"}
# Xe NGƯỢC (short) — Tom lệnh 03/11 gắn PLTZ: nhãn phải ghi rõ NGƯỢC (SHORT)
# để không ai đọc nhầm là xe long như PLTU. Mã gốc chưa có vùng thì không
# dịch số (renderer đã xử lý), có vùng cũng KHÔNG dịch tuyến tính cho xe ngược.
LEV_SHORT = {"PLTZ"}
# 🌙 Xe giao dịch được PHIÊN ĐÊM Robinhood 24 Hour Market (Blue Ocean ATS,
# 20:00 CN → 20:00 T6 ET). Nguồn: Robinhood instrument API field
# all_day_tradability == "tradable", đọc trực tiếp 3/11/2026 (29 mã
# confirmed). KHÔNG có trong set này = confirmed untradable tại Robinhood
# (SNXX, SNDG, MUU, MULL, CONL, CIEG, TSLG, NVDG, INTW) hoặc chưa kiểm —
# cả hai đều KHÔNG gắn nhãn. Danh sách Robinhood thay đổi theo thời gian:
# verify lại trước khi coi là vĩnh viễn.
LEV_24H = {"SOXL", "SOXS", "SQQQ", "SNDU", "CONY", "CONI", "FIAT", "DRAM",
           "RAM", "PLTZ", "PLTU", "AAPU", "AMDL", "AVGX", "COHX", "TSLL",
           "TSLR", "TSLQ", "NVDL", "NVDX", "NVDS", "MVLL", "ROBN", "METU",
           "LRCU", "AAOX", "SKUU", "NBIL", "SMCX",
           # bổ sung 3/11: check nốt toàn bộ xe còn lại trong LEV_MAP qua
           # cùng API — tradable: 16 mã dưới; untradable: CRDU, CRWC,
           # DLLL, KTUP, NOWL, OKTG, RDDC (không gắn nhãn)
           "AMZU", "ASTX", "AVXX", "CEGX", "GGLL", "IONX", "MSFU", "OKLL",
           "ORCU", "RKLX", "SPCH", "STXU", "TNA", "TQQQ", "TSMX", "VSTL",
           # bổ sung 3/11 (lượt gắn KLAG + LITX): check trực tiếp Robinhood
           # API cùng ngày — cả 2 tradable (KLAG phá lệ nhóm Leverage Shares
           # 0/4 trước đó: eligible tính theo từng quỹ, phải check từng mã)
           # bổ sung 3/11 (lượt gắn VRTL): VRTL cũng tradable qua API cùng
           # ngày; CSCL gắn cùng đợt thì untradable — không gắn nhãn
           # bổ sung 3/11 (lượt gắn GLWG): GLWG tradable qua Robinhood API
           # cùng ngày; ANEL/LABX/RTXG/LMTL gắn cùng buổi đều untradable
           # bổ sung 3/11 (lượt gắn STXX ~22:36): STXX check trực tiếp
           # Robinhood API cùng ngày — tradable; STXL gắn ngay sau đó
           # theo lệnh Tom, check cùng lượt — tradable
           "KLAG", "LITX", "VRTL", "GLWG", "STXX", "STXL",
           # bổ sung 4/11 (lượt gắn WDCX theo lệnh Tom): check trực tiếp
           # Robinhood API cùng ngày — WDCX tradable phiên đêm
           "WDCX"}
LEV_SELF = {"SOXL": "ETF x3 bán dẫn", "DRAM": "ETF 1x rổ memory"}
# CONY: Tom chốt LẠI tối 3/11 (~22:50) — gom CONY vào khối COIN, nằm
# ngay dưới hàng CONL vì Tom không muốn phải đi tìm riêng (thay quyết
# định khối riêng độc lập buổi sáng). CONY vẫn KHÔNG phải xe x2: nhãn
# riêng ở LEV_VEH_LABEL và không dịch vùng ở LEV_NODERIVE.
LEV_VEH_LABEL = {"CONY": "QUỸ YIELDMAX THU NHẬP QUYỀN CHỌN TRÊN COIN"
                         " — KHÔNG PHẢI XE x2"}
LEV_NODERIVE = {"CONY": "🎯 Quỹ thu nhập — KHÔNG dịch vùng mua/cắt/"
                        "target theo COIN (đi cùng chiều COIN ~0,85x "
                        "biên độ, NAV mòn dần); chỉ theo dõi giá + cổ "
                        "tức tuần"}
LEV_PAIR_JS = (
    "<script>(function(){function mf(v){var s=String(v),p=s.split('.');"
    "p[0]=p[0].replace(/\\B(?=(\\d{3})+(?!\\d))/g,'.');return '$'+p.join(',');}"
    "function f(){fetch('prices.json?x='+Date.now()).then(function(r){return r.json();})"
    ".then(function(d){var q=d.q||{};"
    "function wsf(t){return window.__wsFresh&&window.__wsFresh[t]&&(Date.now()-window.__wsFresh[t]<90000);}"
    "document.querySelectorAll('[data-px]').forEach(function(e){var v=q[e.getAttribute('data-px')];"
    "if(v&&!wsf(e.getAttribute('data-px')))e.textContent=e.getAttribute('data-px')+' '+mf(v[0]);});"
    "document.querySelectorAll('[data-pct]').forEach(function(e){var v=q[e.getAttribute('data-pct')];"
    "if(v&&!wsf(e.getAttribute('data-pct')))e.textContent='('+(v[1]>0?'+':'')+String(v[1]).replace('.',',')+'%)';});"
    "}).catch(function(){});}"
    "setInterval(f,30000);})();</script>")


def _lev_pair_block(u, vehs, z, st, paused, upx, upct, px):
    lab = {"IN": "🟢 TRONG VÙNG", "BELOW": "🟡 DƯỚI VÙNG",
           "ABOVE": "⚪ TRÊN VÙNG", "BROKEN": "🔴 GÃY VÙNG — THỦNG CẮT",
           "PAUSED": "⏸️ CHỜ DỰNG NỀN — VÙNG TẠM TREO",
           "NOZONE": "⚪ CHƯA CÓ VÙNG"}[st]
    cls = "alert levflash" if st == "IN" else "alert levpair"
    tag = ('<span class="levtag">🔥 ĐANG LỌT VÙNG</span>'
           if st == "IN" else "")
    role = LEV_SELF.get(u, "MÃ GỐC")
    lines = [
        f'<div class="{cls}"><b><span style="background:#f5c86e;color:#131313;'
        f'font-weight:900;padding:1px 7px;border-radius:5px;font-size:1.18em">'
        f'<span data-px="{u}">{u} {_lt_money(upx)}</span></span> '
        f'<span data-pct="{u}">({fmt_pct_vn(upct)})</span> · {role}</b> '
        f'· {lab}{tag}']
    if z and z.get("entry"):
        lo, hi = z["entry"]
        tg = z.get("targets") or []
        cond = " (điều kiện)" if paused else ""
        parts = [f'🎯 VÙNG MUA{cond} {_lt_money(lo)}–{_lt_money(hi)}']
        if z.get("cut"):
            parts.append(f'✂️ CẮT <{_lt_money(z["cut"])}')
        if len(tg) >= 2:
            parts.append(f'🏁 TARGET {_lt_money(tg[0])}→{_lt_money(tg[1])}')
        elif tg:
            parts.append(f'🏁 TARGET {_lt_money(tg[0])}')
        if st == "BELOW" and not paused:
            parts.append(f'còn +{_lt_pct1((lo / upx - 1) * 100)} tới vùng')
        lines.append("<br>" + " · ".join(parts))
    else:
        lines.append("<br>🎯 Chưa có vùng — chỉ theo dõi giá")
    for v in vehs:
        if v not in px:
            continue
        vpx, vpct = px[v]
        mult = "x3" if v in LEV_X3 else "x2"
        if v in LEV_VEH_LABEL:
            vlabel = LEV_VEH_LABEL[v]
        elif v in LEV_SHORT:
            vlabel = f"XE {mult} NGƯỢC (SHORT) CỦA {u}"
        else:
            vlabel = f"XE {mult} CỦA {u}"
        head = (f'⚡ <b><span style="background:#7dd3fc;color:#131313;'
                f'font-weight:900;padding:1px 7px;border-radius:5px;'
                f'font-size:1.12em"><span data-px="{v}">'
                f'{v} {_lt_money(vpx)}</span></span> '
                f'<span data-pct="{v}">({fmt_pct_vn(vpct)})</span></b> '
                f'· {vlabel}'
                + (' <span style="background:#6d5bd0;color:#fff;'
                   'font-weight:800;padding:1px 6px;border-radius:5px">'
                   '🌙 24H</span>' if v in LEV_24H else ''))
        if z and z.get("entry") and upx and v not in LEV_SHORT \
                and v not in LEV_NODERIVE:
            r = vpx / upx
            lo, hi = z["entry"]
            tg = z.get("targets") or []
            cond = " (điều kiện)" if paused else ""
            parts = [f'🎯 VÙNG MUA{cond} {_lt_money(lo * r)}–'
                     f'{_lt_money(hi * r)}']
            if z.get("cut"):
                parts.append(f'✂️ CẮT <{_lt_money(z["cut"] * r)}')
            if len(tg) >= 2:
                parts.append(f'🏁 TARGET {_lt_money(tg[0] * r)}→'
                             f'{_lt_money(tg[1] * r)}')
            elif tg:
                parts.append(f'🏁 TARGET {_lt_money(tg[0] * r)}')
            parts.append(f'<span style="opacity:.7">(dịch theo tỷ lệ '
                         f'{r:.3f})</span>')
            lines.append("<br>" + head + "<br>" + " · ".join(parts))
        elif v in LEV_NODERIVE:
            lines.append("<br>" + head + "<br>" + LEV_NODERIVE[v])
        elif v in LEV_SHORT:
            lines.append("<br>" + head + "<br>🎯 Xe NGƯỢC — chạy ngược mã gốc "
                                "(gốc rớt thì xe tăng); không dịch vùng tuyến "
                                "tính, chỉ daytrade theo đà của mã gốc")
        else:
            lines.append("<br>" + head + "<br>🎯 Mã gốc chưa có vùng — chưa dịch được "
                                "vùng cho xe")
        if v in COMP_WARN:
            lines.append("<br>" + COMP_WARN[v])
    lines.append("</div>")
    return "".join(lines)


def update_leverage_pairs(h, prices):
    """Khối CẶP MÃ GỐC + XE trong sec-leverage (Tom 02/11). Tự dựng lại mỗi
    lượt từ LEV_MAP + zones.json; thay chip strip data-lchip trong mục này.
    Fail-soft: lỗi thì giữ nguyên bảng."""
    try:
        if ".levpair{" not in h:
            css = (".levpair{border:1px solid #2a3648;border-radius:10px;"
                   "padding:7px 10px;margin:0 0 8px;background:#101725}")
            i = h.find("</style>")
            if i > 0:
                h = h[:i] + css + h[i:]
        b = _sec_bounds(h, "sec-leverage")
        if not b:
            return h, 0
        s, e = b
        seg = h[s:e]
        # Tom 02/11 "sửa phải sửa cho hết": mục ĐÒN BẨY chỉ còn KHỐI CẶP —
        # xóa hẳn khối LEV-AUTO cũ của radar (trùng mã, giá snapshot kẹt)
        # mỗi lượt, dù writer nào lỡ ghi lại.
        seg = re.sub(r"<!-- LEV-AUTO -->.*?<!-- /LEV-AUTO -->", "", seg,
                     flags=re.S)
        zones, _excl = _zones_local()
        groups = {}
        for v, u in LEV_MAP.items():
            groups.setdefault(u, [])
            if v != u and v not in groups[u]:
                groups[u].append(v)
        need = set(groups) | {v for vs in groups.values() for v in vs}
        need.add("LNT")
        px = dict(prices)
        missing = [t for t in need if t not in px]
        if missing:
            try:
                got, _l, _h52 = fetch_prices(sorted(missing))
                px.update(got)
            except Exception:
                pass
        order = {"IN": 0, "BELOW": 1, "ABOVE": 2, "PAUSED": 3,
                 "BROKEN": 4, "NOZONE": 5}
        rows = []
        for u, vehs in list(groups.items()) + [("LNT", [])]:
            if u not in px:
                continue
            upx, upct = px[u]
            z = zones.get(u)
            paused = False
            if z and z.get("entry"):
                note = (z.get("note") or "").upper()
                paused = ("TAM VO HIEU" in note or "TEMP-INVALID" in note)
                st = "PAUSED" if paused else _zone_state_of(z, upx)
            else:
                st = "NOZONE"
            rows.append((order[st], u, vehs, z, st, paused, upx, upct))
        rows.sort(key=lambda r: (r[0], r[1]))
        blocks = []
        for _o, u, vehs, z, st, paused, upx, upct in rows:
            blk = _lev_pair_block(u, vehs, z, st, paused, upx, upct, px)
            if u == "LNT":
                blk = blk.replace(
                    "· MÃ GỐC</b>",
                    "· CỔ PHIẾU THƯỜNG (Sổ 1) — Tom xếp canh chung mục này</b>")
            blocks.append(blk)
        inner = "".join(blocks) + LEV_PAIR_JS
        rendered = "<!-- LEV-PAIRS -->" + inner + "<!-- /LEV-PAIRS -->"
        if "<!-- LEV-PAIRS -->" in seg:
            seg = re.sub(r"<!-- LEV-PAIRS -->.*?<!-- /LEV-PAIRS -->",
                         lambda _m: rendered, seg, flags=re.S)
        else:
            # Migration: bỏ chip strip + 2 khối tay INTW/LNT (đã có trong cặp)
            seg = re.sub(r'<div class="zchip [^"]*" data-lchip="[A-Z]+">'
                         r'.*?</div>', "", seg)
            seg = re.sub(r'<div class="alert" style="border:2px solid '
                         r'#f5c86e[^>]*><b>⚡ (?:INTW|LNT) —.*?</div>',
                         "", seg)
            ai = seg.find("<!-- /LEV-AUTO -->")
            pos = ai + len("<!-- /LEV-AUTO -->") if ai >= 0 else len(seg)
            seg = seg[:pos] + rendered + seg[pos:]
        return h[:s] + seg + h[e:], len(blocks)
    except Exception as ex:
        print(f"  ! leverage pairs: {ex}", file=sys.stderr)
        return h, 0



def _sec_bounds(h, sid):
    """(start, end) của 1 section <div class="sec" id="sid">…</div>
    bằng stack-walk (không dùng regex non-greedy — bài học 19/10)."""
    i = h.find('id="%s"' % sid)
    if i < 0:
        return None
    s = h.rfind('<div class="sec"', 0, i)
    if s < 0:
        return None
    depth = 0
    for m in re.finditer(r'</?div\b[^>]*>', h[s:]):
        depth += -1 if m.group(0).startswith('</') else 1
        if depth == 0:
            return s, s + m.end()
    return None


def _dayplan_bounds(h):
    """(start, end) của khối <div class="dayplan" id="dayPlan">…</div>
    bằng stack-walk. dayPlan KHÔNG phải class="sec" nên _sec_bounds
    không dùng được (nó sẽ bắt nhầm section sec đứng trước)."""
    i = h.find('id="dayPlan"')
    if i < 0:
        return None
    s = h.rfind('<div', 0, i)
    if s < 0:
        return None
    depth = 0
    for m in re.finditer(r'</?div\b[^>]*>', h[s:]):
        depth += -1 if m.group(0).startswith('</') else 1
        if depth == 0:
            return s, s + m.end()
    return None


def merge_fresh_sections(h, path):
    """Giữ section của writer khác khỏi bị lượt này ghi đè (race 2/11).
    Đọc lại file tươi ngay trước khi ghi; với sec-crypto và sec-ltalloc —
    2 section quick_refresh KHÔNG sở hữu — nếu bản tươi đầy đủ hơn bản
    trong bộ nhớ thì thay bằng bản tươi. 'Đầy đủ hơn' = đếm marker:
    crypto = số khối alert; ltalloc = số quỹ lta-ret. Lỗi đọc → giữ nguyên.
    BỔ SUNG 2/11 giữa trận: dayPlan cũng được bảo vệ theo cùng luật —
    khối GIỮA TRẬN do worker khác chèn giữa chừng (vd plan-midday-check)
    bị các lượt refresh đọc-stale ghi đè 2 lần trong 1 buổi sáng; nếu
    bản tươi có nhiều alert trong dayPlan hơn bản trong bộ nhớ thì
    giữ nguyên khối dayPlan tươi."""
    try:
        fresh = open(path, encoding="utf-8").read()
    except Exception as e:
        print(f"  ! merge fresh sections: không đọc được {path}: {e}",
              file=sys.stderr)
        return h
    for sid, marker in (("sec-crypto", '<div class="alert'),
                         ("sec-ltalloc", "lta-ret-")):
        try:
            fb = _sec_bounds(fresh, sid)
            hb = _sec_bounds(h, sid)
            if not fb or not hb:
                continue
            fseg, hseg = fresh[fb[0]:fb[1]], h[hb[0]:hb[1]]
            # (3/11 ~23:24: khối tóm tắt lta-flow/lta-count đã bỏ hẳn
            # theo lệnh Tom — không còn guard giữ header ở đây.)
            # FIX 4/11: dòng gán fresher bị xóa nhầm trong commit f6767f10
            # (lượt dọn lta-flow) làm NameError mỗi lượt → section tươi
            # của writer khác bị bỏ qua, crypto/ltalloc bị ghi đè trắng.
            fresher = fseg.count(marker) > hseg.count(marker)
            if fresher:
                h = h[:hb[0]] + fseg + h[hb[1]:]
                print(f"  merge fresh: giữ {sid} bản tươi "
                      f"({fseg.count(marker)} > {hseg.count(marker)} marker)",
                      flush=True)
        except Exception as e:
            print(f"  ! merge fresh {sid}: {e}", file=sys.stderr)
    # dayPlan: khối do worker khác sở hữu (kế hoạch sáng + giữa trận) —
    # cùng luật "bản tươi đầy đủ hơn thì giữ", marker = số khối alert.
    try:
        fb = _dayplan_bounds(fresh)
        hb = _dayplan_bounds(h)
        if fb and hb:
            fseg, hseg = fresh[fb[0]:fb[1]], h[hb[0]:hb[1]]
            if fseg.count('<div class="alert"') > hseg.count('<div class="alert"'):
                h = h[:hb[0]] + fseg + h[hb[1]:]
                print("  merge fresh: giữ dayPlan bản tươi "
                      f"({fseg.count(chr(60)+'div class=\"alert\"')} > "
                      f"{hseg.count(chr(60)+'div class=\"alert\"')} alert)",
                      flush=True)
    except Exception as e:
        print(f"  ! merge fresh dayPlan: {e}", file=sys.stderr)
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
        # 🕸️ LƯỚI THÉP cũng phải sống premarket (Tom bắt lỗi 26/10: mục onsale
        # đông cứng ở giá đóng cửa thứ Sáu 13:05 suốt premarket vì lite bỏ qua).
        # Giá premarket thật + đỉnh 52w từ cache của lượt full gần nhất (đỉnh
        # 52w biến chậm, cache là chấp nhận được). Chỉ cập nhật khi lấy đủ giá —
        # fetch lỗi thì GIỮ bảng cũ, tuyệt đối không xóa trắng section.
        try:
            _h52c = {}
            _cf = os.path.join(a.repo_dir, "highs52_cache.json")
            if os.path.exists(_cf):
                _h52c = json.load(open(_cf))
            _sec = re.search(r'id="sec-steelnet">(.*?)</div>\n', h, re.S)
            _rows = re.findall(r"<tr><td><b>([A-Z]{2,5})</b></td>",
                               _sec.group(1)) if _sec else []
            _uni = sorted(set(ZONES.keys()) | STEEL_QUALITY | set(_rows))
            _spm = fetch_pm_prices(_uni)
            if len(_spm) >= 10 and _h52c:
                h, _nsteel = steel_net_update(h, _spm, _h52c, pm=True)
                print(f"lite premarket: {_nsteel} (giá premarket)", flush=True)
            else:
                print(f"lite premarket: steelnet giữ nguyên (pm={len(_spm)}, "
                      f"cache={len(_h52c)})", flush=True)
        except Exception as _e:
            print(f"lite premarket: steelnet skip ({_e})", flush=True)
        try:
            _chip_px = {}
            for _d in (_spm, _pm):
                try:
                    _chip_px.update(_d)
                except Exception:
                    pass
            if _chip_px:
                # ĐỒNG BỘ SỐ 04/11: lite phải phủ giá cho MỌI mã có số
                # in trong các mục đích — chỉ bằng fetch_pm_prices
                # (NGUỒN DUY NHẤT ngoài giờ). Thiếu mã, updater con tự
                # fetch bằng fetch_prices (giá ĐÓNG CỬA) -> trộn 2
                # nguồn trong 1 lượt = AMD 3 nơi 3 số. Bổ sung tại đây.
                try:
                    _tgt = set(COMP_MAP) | set(COMP_MAP.values()) \
                        | set(LEV_MAP) | set(LEV_MAP.values()) \
                        | set(DAY_HOT_POOL) | {"LNT"}
                    for _sid in SYNC_SECS + ("sec-tinhhoa",):
                        _bb = _sec_bounds(h, _sid)
                        if _bb:
                            _sg = h[_bb[0]:_bb[1]]
                            _tgt |= set(re.findall(
                                r'data-(?:px|pct|achip|lchip|fchip)='
                                r'"([A-Z]{1,6})"', _sg))
                            _tgt |= set(re.findall(
                                r"<tr><td><b>([A-Z]{2,6})</b></td>",
                                _sg))
                    _miss = [t for t in sorted(_tgt) if t not in _chip_px]
                    if _miss:
                        _chip_px.update(fetch_pm_prices(_miss))
                except Exception as _e2:
                    print(f"lite premarket: pm universe skip ({_e2})",
                          flush=True)
                h, _nh1 = update_hot_entries(h, _chip_px)
                h, _nh2 = update_tables(h, _chip_px)
                h, _nh3 = update_dash_prices(h, _chip_px)
                h, _nday = rebuild_daytrade(h, _chip_px)
                h, _nch = update_zone_chips(h, _chip_px)
                h, _nlv = update_leverage_signals(h, _chip_px)
                h, _nlp = update_leverage_pairs(h, _chip_px)
                h, _ncomp, _compc = update_companion_lines(h, _chip_px)
                h, _nfch = update_focus_chips(h, _chip_px)
                h, _nach = update_alert_chips(h, _chip_px)
                h, _ncln2 = clean_shark_layers(h)
                if _ncln2:
                    print(f"lite premarket: dọn lớp cá mập {', '.join(_ncln2)}",
                          flush=True)
                h, _nlt = update_ltalloc_signals(h, _chip_px)
                h, _nsync = sync_static_numbers(h, _chip_px)
                print(f"lite premarket: chips {_nch} bảng + {_nfch} FOCUS"
                      f" + {_nach} alert + daytrade {_nday} khối"
                      f" + sync {_nsync} chỗ", flush=True)
        except Exception as _e:
            print(f"lite premarket: chips skip ({_e})", flush=True)
        try:
            _allpx = {}
            for _d in (_chip_px, _pm, _spm):
                try:
                    _allpx.update(_d)
                except Exception:
                    pass
            write_prices_cache(a.repo_dir, _allpx)
        except Exception:
            pass
        h = bull_bear_update(h, a.repo_dir)
        h = render_regime_strip(h, a.repo_dir)
        h = ensure_compact_css(h)
        h = board_compact(h)
        h = ensure_lang_toggle(h)
        h = ensure_share_gate(h)
        h = ensure_ws_live(ensure_yahoo_live(h))
        h = bump_badge(h, "premarket — Bull & Bear theo futures")
        ok, why = validate(h)
        if not ok:
            print(f"VALIDATE FAIL: {why}", file=sys.stderr)
            return 2
        open(a.html, "w", encoding="utf-8").write(h)
        repo_html = os.path.join(a.repo_dir, "index.html")
        if os.path.abspath(a.html) != os.path.abspath(repo_html):
            open(repo_html, "w", encoding="utf-8").write(h)
        h = dash_top_merge(h)
        h, _ = stamp_version(h, a.repo_dir)
        open(a.html, "w", encoding="utf-8").write(h)
        if os.path.abspath(a.html) != os.path.abspath(repo_html):
            open(repo_html, "w", encoding="utf-8").write(h)
        res = git_push(a.repo_dir,
                       f"refresh {now_pt().strftime('%d/%m %H:%M')} PT (lite premarket)")
        print(f"lite premarket: bull&bear + fear gauge tươi | git: {res}",
              flush=True)
        publish_yahoo_live()
        return 0 if res in ("pushed", "nothing-to-commit") else 3

    load_zones(a.repo_dir)

    tickers = discover_tickers(open(a.html, encoding="utf-8").read())
    print(f"quét thấy {len(tickers)} mã trên bảng", flush=True)
    print(f"lấy giá {len(tickers)} mã...", flush=True)
    prices, daylows, highs52 = fetch_prices(tickers)
    print(f"ok {len(prices)}/{len(tickers)}", flush=True)
    # Cache đỉnh 52w cho lượt lite premarket dùng (steelnet sống ngoài giờ).
    try:
        json.dump(highs52, open(os.path.join(a.repo_dir, "highs52_cache.json"), "w"))
    except Exception:
        pass
    if not prices:
        print("không lấy được giá nào — thoát", file=sys.stderr)
        return 1

    h = open(a.html, encoding="utf-8").read()
    # header ngày của cột giá theo đúng hôm nay (fix 14/10: kẹt "Giá 13/10")
    h = re.sub(r"<th>Giá \d{2}/\d{2}</th>",
               f"<th>Giá {now_pt().strftime('%d/%m')}</th>", h)
    h, n1 = update_hot_entries(h, prices)
    h, n2 = update_tables(h, prices)
    # Khuôn "<b>MÃ</b> — $X (Y%)" (sec-ltalloc/sec-lt9 — Tom lệnh giá real-time
    # trong từng ô LONG TERM 29/10): mã nào chưa có giá (vd SGOV không nằm ở
    # bảng/hot entries nào) thì fetch bổ sung, lỗi thì updater tự giữ nguyên.
    _dash_t = sorted(set(m.group(2) for m in DASH_PRICE_PAT.finditer(h)))
    _dash_missing = [t for t in _dash_t if t not in prices]
    if _dash_missing:
        try:
            _dx, _, _ = fetch_prices(_dash_missing)
            prices = {**prices, **_dx}
        except Exception as e:
            print(f"  ! dash prices extra: {e}", file=sys.stderr)
    h, n3 = update_dash_prices(h, prices)
    h, n4 = update_ltalloc_signals(h, prices)
    if n4:
        print(f"  ltalloc signals: {n4} ô cập nhật màu theo giá", flush=True)
    h, n5 = update_zone_chips(h, prices)
    h, nlv = update_leverage_signals(h, prices)
    h, ncomp, compc = update_companion_lines(h, prices)
    if n5:
        print(f"  zone chips: {n5} khung đầu hàng Sổ 1/2/3", flush=True)
    print(f"đã sửa {n1} hot entries, {n2} table cells, {n3} dash prices",
          flush=True)

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
    # 🧭 LUẬT CHẾ ĐỘ — tính đèn từ nến ngày hoàn tất TRƯỚC khâu tín hiệu khẩn:
    # 🔴 PHÒNG THỦ thì refresh_urgent tự tắt toàn bộ alert mua mới.
    _regime_st = compute_regime(a.repo_dir)
    _regime = _regime_st.get("regime") if isinstance(_regime_st, dict) else None
    print(f"chế độ thị trường: {_regime_st.get('label') if isinstance(_regime_st, dict) else '?'}",
          flush=True)
    load_trust(a.repo_dir)  # 🎓 điểm tin cậy 30 ngày cho alert khẩn
    h, nu = refresh_urgent(h, all_px, all_low, zone_events, a.repo_dir, flows, caps, revs,
                           regime=_regime)
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
    # Tom bắt lỗi 30/10: cùng mã lặp 2–3 lần trong swing/daytrade/dram —
    # mỗi mã chỉ giữ alert MỚI NHẤT trong các mục tín hiệu này.
    _cd = []
    for _sec in ("sec-swing", "sec-daytrade", "sec-dram"):
        h, _cx = dedup_alert_section(h, _sec)
        _cd += [f"{t}@{_sec}" for t in _cx]
    if c1 or c2 or c3 or _cd:
        print(f"tự dọn trùng: {', '.join(c1 + c2 + c3 + _cd)}", flush=True)

    # 🔥 HOT FEED TĨNH — dựng sẵn nội dung #hotFeed để trang riêng cũng có
    # mã (JS buildHotFeed chỉ chạy trên bảng chính). Chạy sau dedup để
    # nguồn alert đã sạch trùng.
    h, nhf = build_hot_feed(h, prices)
    print(f"hot feed tĩnh: {nhf}", flush=True)
    h, nls = build_live_strip(h, prices)
    print(f"live strip tĩnh: {nls}", flush=True)

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

    # 🧭 Dải đèn CHẾ ĐỘ — nằm ngay dưới Bull & Bear (Luật Chế Độ 23/10)
    h = render_regime_strip(h, a.repo_dir)

    # DAYTRADE dựng lại từ giá hiện tại TRƯỚC chặng 2 để companion/
    # alert-chip gắn tiếp lên khối mới (Tom lệnh 04/11: mâm daytrade
    # phải đầy đủ ~18–22 khối, số từ nguồn duy nhất của lượt).
    h, _nday = rebuild_daytrade(h, {**prices, **nhip_extra})
    if _nday:
        print(f"  daytrade: dựng lại {_nday} khối mã nóng", flush=True)

    # KHUNG ĐẦU KHỐI chặng 2 (Tom 30/10) — chạy LẠI cuối pipeline để phủ
    # lưới thép vừa dựng lại + ETF16 + dòng Sổ ensure vừa thêm + FOCUS.
    h, nch2 = update_zone_chips(h, {**prices, **nhip_extra})
    h, nlv2 = update_leverage_signals(h, {**prices, **nhip_extra})
    h, npr = update_leverage_pairs(h, {**prices, **nhip_extra})
    print(f"  leverage pairs: {npr} khối cặp mã gốc + xe", flush=True)
    h, ncomp2, compc2 = update_companion_lines(h, {**prices, **nhip_extra})
    h, nfch = update_focus_chips(h, {**prices, **nhip_extra})
    h, nach = update_alert_chips(h, {**prices, **nhip_extra})
    h, _ncln = clean_shark_layers(h)
    if _ncln:
        print(f"  dọn lớp cá mập: {', '.join(_ncln)}", flush=True)
    print(f"  zone chips chặng 2: {nch2} bảng + {nfch} FOCUS + {nach} alert",
          flush=True)
    print(f"  companion xe x2: {ncomp2} dòng {compc2}", flush=True)

    # ĐỒNG BỘ SỐ TĨNH — pass cuối: mọi số in còn sót trong các mục
    # đích tính lại từ cùng bộ giá của lượt + zones.json (04/11).
    h, _nsync = sync_static_numbers(h, {**prices, **nhip_extra})
    print(f"  đồng bộ số tĩnh: {_nsync} chỗ sửa", flush=True)

    h = ensure_compact_css(h)
    h = board_compact(h)
    h = ensure_lang_toggle(h)
    h = ensure_share_gate(h)
    h = ensure_ws_live(ensure_yahoo_live(h))
    write_prices_cache(a.repo_dir, {**prices, **nhip_extra})
    h = bump_badge(h, tag)
    # CHỐNG GHI ĐÈ RACE (Tom 2/11 — 2 lần clobber trong 1 buổi sáng):
    # lượt này đọc file từ đầu lượt; nếu giữa chừng có writer khác (crypto/
    # ltalloc) khôi phục section mà lượt này không sở hữu, bản ghi cuối sẽ
    # xóa sạch công khôi phục đó. Trước khi ghi: đọc lại file TƯƠI, giữ lại
    # section đầy đủ hơn cho sec-crypto (khối coin) và sec-ltalloc (hàng quỹ).
    h = merge_fresh_sections(h, a.html)
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
    h = dash_top_merge(h)
    h, _ = stamp_version(h, a.repo_dir)
    open(a.html, "w", encoding="utf-8").write(h)
    if os.path.abspath(a.html) != os.path.abspath(repo_html):
        open(repo_html, "w", encoding="utf-8").write(h)

    msg = f"refresh {now_pt().strftime('%d/%m %H:%M')} PT ({tag})"
    res = git_push(a.repo_dir, msg)
    print("git:", res, flush=True)
    publish_yahoo_live()
    return 0 if res in ("pushed", "nothing-to-commit") else 3


if __name__ == "__main__":
    sys.exit(main())
