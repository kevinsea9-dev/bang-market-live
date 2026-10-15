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
        low = float(d.get("regularMarketDayLow") or 0)
        return ticker, (round(price, 2), round(pct, 2), round(low, 2))
    except Exception as e:
        print(f"  ! {ticker}: {e}", file=sys.stderr)
        return ticker, None


def fetch_prices(tickers):
    """Trả về (prices, daylows): prices[t]=(giá, %) giữ nguyên để code cũ không vỡ;
    daylows[t]=đáy ngày phục vụ dò bounce."""
    out, lows = {}, {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t, v in ex.map(fetch_one, tickers):
            if v:
                out[t] = (v[0], v[1])
                if v[2]:
                    lows[t] = v[2]
    return out, lows


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


def auto_cleanup(h, prices, tickers, repo_dir):
    """Tom lệnh 14/10: mã nào trùng / chết (không update được) thì TỰ BIẾN MẤT cho đúng Live.
    - Chết: fetch fail 3 lượt liên tiếp -> xóa dòng.
    - Trùng: giữ dòng đầu, xóa các dòng sau."""
    removed = []
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

    # Cú Đêm: chỉ giữ 3 mới nhất
    def trim_cudem(m):
        header, body = m.group(1), m.group(2)
        alerts = re.findall(r'(<div class="alert[^"]*">.*?</div>)\s*', body, re.S)
        if len(alerts) > 3:
            removed[0] += len(alerts) - 3
            return header + '\n'.join(alerts[:3]) + '\n</div>\n'
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


def refresh_urgent(h, prices, daylows, events, repo_dir, flows=None):
    date_s = now_pt().strftime("%d/%m")
    cands = []  # (key, ticker, html)
    for t, (px, pc) in prices.items():
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
        if r >= 2.5 and b >= 3.0:
            cands.append((f"UMF{t}", t,
                          f"💰 <b>{t} DÒNG TIỀN VÀO</b> — vol {r}x TB, bounce +{b:.1f}% từ đáy"))
    for ev in events or []:
        t, _prev, st, px, _pc = ev[0], ev[1], ev[2], ev[3], ev[4]
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
        fresh.append((mark, t, html))
        if len(fresh) >= URGENT_MAX:
            break
    if not fresh:
        return h, "khẩn: không có signal mới (giữ alert cũ)"
    for mark, t, html in fresh:
        px = prices.get(t, (0, 0))[0]
        journal_signal(repo_dir, "urgent", t, px, html[:60])
    new_alerts = "".join(
        f'{mark}<div class="alert urgent"><b>{html}</b><br>'
        f'<span class="t">Tín hiệu khẩn • {date_s} • chớp nhanh, tiền đang chạy • chỉ tham khảo</span></div>\n'
        for mark, _, html in fresh)
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
    names = ", ".join(t for _, t, _ in fresh)
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
        "quick_refresh.py", "refresh-tickers.txt", "zones.json", "fetch_failures.json")
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

    load_zones(a.repo_dir)

    tickers = discover_tickers(open(a.html, encoding="utf-8").read())
    print(f"quét thấy {len(tickers)} mã trên bảng", flush=True)
    print(f"lấy giá {len(tickers)} mã...", flush=True)
    prices, daylows = fetch_prices(tickers)
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
        nhip_extra, nhip_lows = fetch_prices(NHIP_EXTRA)
    except Exception as e:
        print(f"  ! nhip extra: {e}", file=sys.stderr)
        nhip_extra, nhip_lows = {}, {}
    # 💪 lấy % SPY để dò relative strength (mã khỏe hơn market)
    try:
        _mkt, _ = fetch_prices(["SPY"])
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

    # tự dọn TIN CŨ — Tom lệnh 15/10: tin cũ phải biến mất, xử tự động mỗi 5 phút
    h, n_pruned = prune_old_alerts(h)
    if n_pruned:
        print(f"dọn tin cũ: {n_pruned} tin", flush=True)

    # BẢN ĐỒ SECTOR — dòng tiền đi đâu, mỗi lượt
    try:
        sec_prices, _ = fetch_prices([etf for _, etf in SECTOR_CARDS])
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
        zone_events = events
        if events:
            h = add_signals(h, events)
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

    # 🚨 TÍN HIỆU KHẨN — Tom lệnh 15/10: chớp nháy khẩn cho mọi người kiếm tiền
    # PRO: quét toàn vũ trụ (không chỉ 51 mã bảng) + tầng BOUNCE MẠNH + DÒNG TIỀN
    h, nu = refresh_urgent(h, all_px, all_low, zone_events, a.repo_dir, flows)
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
    if c1 or c2:
        print(f"tự dọn trùng: {', '.join(c1 + c2)}", flush=True)

    if a.mode == "close":
        h = close_headers(h)
        tag = "chốt phiên"
        print("đã đổi header sang đóng cửa", flush=True)

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
