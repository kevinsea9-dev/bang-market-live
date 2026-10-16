#!/usr/bin/env python3
"""📡 BOUNCE RADAR 3 PHÚT — Tom lệnh 15/10: signal chậm = đu đỉnh.
Quét nhẹ các mã đang yếu trong radar_watchlist.json (đỏ ≥3%), bắt tier SỚM:
  🌱 NGÓC ĐẦU: đang đỏ ≥4% + hồi ≥1.5% từ đáy (SỚM, chưa xác nhận — rủi ro cao hơn)
  🔄 XÁC NHẬN: hồi ≥3% từ đáy
  🚨 MẠNH: hồi ≥5% từ đáy
  🔥 RẤT MẠNH: hồi ≥8% từ đáy
Mỗi ticker mỗi tier báo 1 lần/ngày (watermark radar_state.json).
KHÔNG viết bảng, KHÔNG git — chỉ in signal để worker báo chat Tom ngay lập tức.
Trade-off trung thực: tier sớm bắt được giá tốt nhưng có false start;
tier xác nhận chắc hơn nhưng giá xấu hơn. Báo rõ tier để Tom tự cân.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "Mozilla/5.0"}
REPO = os.path.expanduser("~/workspace/market-board")


def now_pt():
    return datetime.now(PT)


def fetch_one(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
    try:
        req = Request(url, headers=UA)
        with urlopen(req, timeout=8) as r:
            d = json.load(r)["chart"]["result"][0]["meta"]
        px = float(d["regularMarketPrice"])
        prev = float(d.get("chartPreviousClose") or d.get("previousClose") or px)
        pc = (px - prev) / prev * 100 if prev else 0
        low = float(d.get("regularMarketDayLow") or 0)
        return ticker, px, pc, low
    except Exception as e:
        print(f"  ! {ticker}: {e}", file=sys.stderr)
        return ticker, None, None, None


def fetch_vol_one(ticker):
    """Volume hôm nay (annualized) / TB 20 phiên. Chỉ dùng cho mã ứng viên sprout."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1mo"
        req = Request(url, headers=UA)
        with urlopen(req, timeout=8) as r:
            res = json.load(r)["chart"]["result"][0]
        vols = [v for v in res["indicators"]["quote"][0]["volume"] if v]
        if len(vols) < 22:
            return None
        avg = sum(vols[-21:-1]) / 20
        today = vols[-1]
        n = now_pt()
        el = max(0.1, min((n.hour * 60 + n.minute - 390) / 390, 1.0))
        return round((today / el) / avg, 1) if avg else None
    except Exception:
        return None


def main():
    n = now_pt()
    if n.weekday() >= 5:
        return
    hm = n.hour * 60 + n.minute
    if not (390 <= hm <= 780):  # 06:30–13:00 PT
        return
    wl_path = os.path.join(REPO, "radar_watchlist.json")
    if not os.path.exists(wl_path):
        print("radar: chưa có watchlist (chờ lượt full)")
        return
    tickers = json.load(open(wl_path)).get("tickers", [])[:25]
    if not tickers:
        print("radar: watchlist rỗng — market không có mã yếu")
        return
    today = n.strftime("%Y-%m-%d")
    st_path = os.path.join(REPO, "radar_state.json")
    state = {}
    try:
        if os.path.exists(st_path):
            state = json.load(open(st_path))
    except Exception:
        pass
    if state.get("date") != today:
        state = {"date": today, "seen": {}}
    seen = state["seen"]
    lows_hist = state.get("lows", {})
    sigs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(fetch_one, tickers))
    # ứng viên ngóc đầu → kiểm tra volume (chỉ mã này mới cần, tiết kiệm request)
    sprout_cands = []
    rows = []
    for t, px, pc, low in results:
        if px is None or not low:
            continue
        b = (px - low) / low * 100
        rows.append((t, px, pc, low, b))
        # 🔻 GÃY ĐÁY — phá đáy mới trong ngày = dao đang rơi (bảo quản vốn)
        prev_low = lows_hist.get(t)
        if prev_low and low < prev_low * 0.99:
            mark = f"{t}:breakdown"
            if mark not in seen:
                seen[mark] = n.isoformat()
                sigs.append(f"🔻 <b>{t} GÃY ĐÁY MỚI</b> ${low:.2f} (đáy cũ ${prev_low:.2f}) — dao đang rơi, đứng ngoài")
        lows_hist[t] = low
        if pc <= -4.0 and b >= 1.5 and b < 3.0:
            sprout_cands.append(t)
    vol_map = {}
    if sprout_cands:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for t, r in zip(sprout_cands, ex.map(fetch_vol_one, sprout_cands)):
                if r:
                    vol_map[t] = r
    for t, px, pc, low, b in rows:
        # chỉ báo tier CAO NHẤT đạt được — tránh spam 3-4 tin cho cùng 1 mã
        tier = None
        if b >= 8.0:
            tier = ("verystrong",
                    f"🔥 <b>{t} RẤT MẠNH</b> +{b:.1f}% từ đáy (đang {pc:+.1f}%)")
        elif b >= 5.0:
            tier = ("strong",
                    f"🚨 <b>{t} MẠNH</b> +{b:.1f}% từ đáy (đang {pc:+.1f}%)")
        elif b >= 3.0:
            tier = ("confirm",
                    f"🔄 <b>{t} XÁC NHẬN</b> +{b:.1f}% từ đáy (đang {pc:+.1f}%)")
        elif pc <= -4.0 and b >= 1.5:
            r = vol_map.get(t)
            # 🌱💰 ngóc đầu CÓ VOLUME = tín hiệu bắt đáy chất lượng cao
            if r and r >= 1.5:
                tier = ("sprout_vol",
                        f"🌱💰 <b>{t} NGÓC ĐẦU CÓ VOLUME</b> +{b:.1f}% từ đáy, vol {r}x (đang {pc:+.1f}%) — tay to vào sớm")
            else:
                tier = ("sprout",
                        f"🌱 <b>{t} NGÓC ĐẦU</b> +{b:.1f}% từ đáy (đang {pc:+.1f}%) — SỚM, chưa xác nhận")
        if tier:
            key, html = tier
            mark = f"{t}:{key}"
            if mark not in seen:
                seen[mark] = n.isoformat()
                sigs.append(html)
    state["seen"] = seen
    state["lows"] = lows_hist
    json.dump(state, open(st_path, "w"))
    if sigs:
        print("RADAR SIGNAL:")
        for s in sigs:
            print(" ", s)
    else:
        print(f"radar: quét {len(tickers)} mã yếu, không có tier mới")


if __name__ == "__main__":
    main()
