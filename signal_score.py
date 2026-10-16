#!/usr/bin/env python3
"""SIGNAL SCORE — Tom lệnh 15/10/2026: script phải HỌC từ signal của chính nó.
Đọc signal_journal.jsonl, chấm điểm từng signal (giá đóng cửa vs giá lúc báo),
tổng hợp theo loại signal 30 ngày gần nhất -> market_state.json["signal_score"].
quick_refresh.py đọc và hiện dòng 🎓 trên bảng mỗi 5 phút.
Chạy lúc chốt phiên (13:05 PT) khi giá đã đóng cửa.
"""
import json, os, sys, datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PT = datetime.timezone(datetime.timedelta(hours=-7))
WIN_BAR = {"bounce": 0.5, "bounce_watch": 0.0, "gap": 0.5}


def now_pt():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(PT)


def close_price(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d"
    try:
        req = Request(url, headers=UA)
        with urlopen(req, timeout=10) as r:
            d = json.load(r)["chart"]["result"][0]
        return float(d["meta"]["regularMarketPrice"])
    except Exception as e:
        print(f"  ! {ticker}: {e}", file=sys.stderr)
        return None


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/workspace/market-board")
    jp = os.path.join(repo, "signal_journal.jsonl")
    if not os.path.exists(jp):
        print("chưa có journal — thoát")
        return 0
    recs = [json.loads(l) for l in open(jp, encoding="utf-8") if l.strip()]
    today = now_pt().strftime("%Y-%m-%d")
    # chấm các signal chưa có outcome và đã qua ngày (giá đóng cửa đã chốt)
    todo = [r for r in recs if "ret" not in r and r["date"] < today]
    # + signal hôm nay (chấm bằng giá đóng cửa vừa chốt)
    todo += [r for r in recs if "ret" not in r and r["date"] == today]
    tickers = sorted(set(r["ticker"] for r in todo))
    closes = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t, px in zip(tickers, ex.map(close_price, tickers)):
            if px:
                closes[t] = px
    n_scored = 0
    for r in todo:
        px = closes.get(r["ticker"])
        if not px or not r.get("price"):
            continue
        ret = (px - r["price"]) / r["price"] * 100
        r["ret"] = round(ret, 2)
        r["close"] = round(px, 2)
        bar = WIN_BAR.get(r["type"], 0.5)
        r["win"] = ret >= bar
        n_scored += 1
    with open(jp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")

    # tổng hợp 30 ngày
    cutoff = (now_pt() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    agg = {}
    for r in recs:
        if r["date"] < cutoff or "win" not in r:
            continue
        a = agg.setdefault(r["type"], {"n": 0, "w": 0, "ret_sum": 0.0})
        a["n"] += 1
        a["w"] += 1 if r["win"] else 0
        a["ret_sum"] += r["ret"]
    by_type = {}
    parts = []
    for t, a in sorted(agg.items()):
        wr = a["w"] / a["n"] * 100 if a["n"] else 0
        avg = a["ret_sum"] / a["n"] if a["n"] else 0
        by_type[t] = {"n": a["n"], "winrate": round(wr, 1), "avg_ret": round(avg, 2)}
        parts.append(f"{t} {wr:.0f}% ({a['w']}/{a['n']}, {avg:+.1f}% TB)")
    line = "🎓 Điểm signal 30 ngày: " + (" • ".join(parts) if parts else "chưa đủ dữ liệu")
    ms_path = os.path.join(repo, "market_state.json")
    ms = {}
    if os.path.exists(ms_path):
        try:
            ms = json.load(open(ms_path, encoding="utf-8"))
        except Exception:
            ms = {}
    ms["signal_score"] = {"updated": today, "by_type": by_type, "line": line}
    json.dump(ms, open(ms_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"chấm {n_scored} signal | {line}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
