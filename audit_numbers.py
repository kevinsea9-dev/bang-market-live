#!/usr/bin/env python3
"""🔍 AUDIT SỐ LIỆU BẢNG LIVE — Tom bắt lỗi 04/11/2026 05:36 PT.

Chuẩn kiểm MỚI (luật 04/11): "kiểm tra bảng" = audit MỌI giá in trên
TỪNG mục so với NGUỒN DUY NHẤT (prices.json + zones.json) ngay thời điểm
kiểm. Version pair khớp + div cân bằng + mục hiện diện KHÔNG phải kiểm
tra số.

Cách làm: tải HTML (live hoặc file) + prices.json tươi, trích mọi mẫu
"MÃ $giá" trong từng sec-*, phân loại:
  - số VÙNG (Vào/Chốt/Cắt/Vùng/Target của chính mã đó) -> đối chiếu
    zones.json (bắt vụ MU swing còn vùng cũ $890–920).
  - số GIÁ (còn lại) -> đối chiếu prices.json (bắt vụ AMD 3 nơi 3 số,
    leverage đóng băng giá đóng cửa).
Xuất báo cáo theo mục: số mã kiểm, số mã lệch >1%, lệch nặng nhất.
Dùng: python3 audit_numbers.py [--html URL|FILE] [--prices URL|FILE]
      [--out FILE.json] [--quiet]   (exit 1 nếu có mục LỆCH NẶNG)
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_BASE = "https://kevinsea9-dev.github.io/bang-market-live"
TARGET_SECS = ("sec-daytrade", "sec-swing", "sec-longterm", "sec-so2",
               "sec-so3", "sec-leverage", "sec-special", "sec-topfocus",
               "sec-plan", "sec-hot", "sec-tinhhoa", "sec-watch",
               "sec-steelnet", "sec-etf", "sec-etf16", "sec-ltalloc",
               "sec-lt9", "sec-news", "sec-urgent", "sec-ahshark")
ZONE_WORDS = ("vùng", "vào", "chốt", "cắt", "target", "trong vùng",
              "trên vùng", "dưới vùng", "mép")


def fetch(url_or_path, tries=3):
    if os.path.exists(url_or_path):
        with open(url_or_path, encoding="utf-8") as f:
            return f.read()
    url = url_or_path
    if "?" not in url:
        url += f"?cb={int(time.time())}"
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # Pages qua proxy hay treo — thử lại
            last = e
            time.sleep(8)
    raise RuntimeError(f"fetch fail {url_or_path}: {last}")


def parse_num(s):
    """'$1.234,56' VN / '$1,234.56' US / '$483,9' VN / '$855.10' US -> float."""
    s = s.strip().replace(" ", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s):      # VN nghìn chấm
        return float(s.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):      # US nghìn phẩy
        return float(s.replace(",", ""))
    if re.fullmatch(r"\d+,\d{1,2}", s):                   # VN thập phân phẩy
        return float(s.replace(",", "."))
    if re.fullmatch(r"\d+(\.\d+)?", s):                   # nguyên / US thập phân
        return float(s)
    return None


def strip_noise(html):
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    return html


def sections(html):
    """{sec_id: text} — cắt theo vị trí <div class="sec" id=...> kế tiếp."""
    marks = [(m.group(1), m.start())
             for m in re.finditer(r'<div class="sec" id="(sec-[a-z0-9\-]+)"',
                                  html)]
    out = {}
    for i, (sid, st) in enumerate(marks):
        en = marks[i + 1][1] if i + 1 < len(marks) else len(html)
        out[sid] = html[st:en]
    if marks:  # section cuối: bound bằng stack-walk, tránh nuốt
        # footer/nội dung đuôi trang vào kết quả audit của mục đó
        sid, st = marks[-1]
        depth = 0
        for dm in re.finditer(r"</?div\b[^>]*>", html[st:]):
            depth += -1 if dm.group(0).startswith("</") else 1
            if depth == 0:
                out[sid] = html[st:st + dm.end()]
                break
    return out


def zone_numbers(z):
    vals = set()
    if not z:
        return vals
    for v in (z.get("entry") or []):
        vals.add(float(v))
    for v in (z.get("targets") or []):
        vals.add(float(v))
    if z.get("cut"):
        vals.add(float(z["cut"]))
    return vals


def audit(html, prices, zones):
    """prices: {T: [px, pct]}; zones: zones.json dict. Trả dict báo cáo."""
    secs = sections(strip_noise(html))
    report = {}
    # Chỉ khớp mã CÓ trong prices.json — tránh mã giả ('PT' trong
    # '03:39 PT') nuốt mất match của mã thật đứng sau nó.
    ticks = sorted((t for t in prices if len(t) >= 2),
                   key=len, reverse=True)
    pat = re.compile(r"\b(" + "|".join(map(re.escape, ticks)) + r")\b"
                     r"([^$%]{0,30}?)\$\s*([\d][\d.,]*)")
    for sid, seg in secs.items():
        txt = re.sub(r"<[^>]+>", " ", seg)
        txt = txt.replace("&lt;", "<").replace("&gt;", ">")
        txt = re.sub(r"\s+", " ", txt)
        checked, bad, zbad = {}, [], {}
        for m in pat.finditer(txt):
            t, mid, nums = m.group(1), m.group(2), m.group(3)
            if t not in prices:
                continue
            # Câu nhiều mã: giá thuộc về mã GẦN NHẤT phía trước $
            # (tránh gán giá RCL cho RTX khi cả 2 cùng xuất hiện).
            _pre = m.group(0).split("$")[0]
            _toks = [x for x in re.findall(r"\b([A-Z]{2,6})\b", _pre)
                     if x in prices]
            if _toks and _toks[-1] != t:
                t = _toks[-1]
                mid = _pre[_pre.rfind(t) + len(t):]
            val = parse_num(nums)
            if val is None or val <= 0:
                continue
            low_mid = mid.lower()
            if any(w in low_mid for w in
                   ("đóng", "ma20", "ma50", "ma200", "kệ", "đáy",
                    "đỉnh", "hỗ trợ", "kháng cự", "đồng thuận",
                    "rơi từ", "rơi về")):
                continue  # số có nhãn kỹ thuật/lịch sử (MA200, giá
                # đóng cửa...) — không phải giá hiện tại, bỏ qua
            # 11/11: "Đóng 2/11: MRVL $171,41 ..., LITE $684,40" — giá
            # đóng cửa có ngày đứng TRƯỚC mã (ngoài mid) nên luật trên
            # không bắt được; LRCU "$260,81" cũng là giá LRCX cũ trong
            # câu "rơi từ $260,81" (đã chặn bằng "rơi từ" ở trên). Cả 2
            # báo oan lệch 916%/3,8% ở sec-so2 ngày 11/11.
            _pre60 = txt[max(0, m.start() - 60):m.start()]
            if (re.search(r"đóng\s+\d{1,2}/\d{1,2}", _pre60, re.I)
                    and "giá" not in _pre60.lower()
                    and "giá" not in low_mid):
                continue  # giá đóng cửa ghi ngày — không phải giá hiện tại
            if "eps" in low_mid:
                continue  # EPS thật/kỳ vọng ("BEAT EPS $0.81") — không
                # phải giá (vụ 05/11: tinhhoa 4 mã "lệch 94–99%")
            if any(w in low_mid for w in
                   ("nâng", "hạ pt", "price target", "mục tiêu")):
                continue  # giá mục tiêu analyst ("VST được nâng PT lên
                # $180") — không phải giá hiện tại (vụ 09/11: VST
                # lệch 42% giả, giá thật trên chip $126,24 đúng)
            if mid.rstrip().endswith("~"):
                continue  # số ước lượng "~$35" (tham chiếu Sổ) — không
                # phải giá hiện tại (vụ 05/11: IREN ~$35 vs live $20.93)
            # 11/11: "$26B" (tỷ đô — vd "private placement $26B") là số
            # tiền, không phải giá; "MA200 SPY $730,99 (đóng 10/11)" là
            # giá trị MA có ngày đóng cửa ngay sau số, không phải giá
            # hiện tại (sec-plan: MA200 lệch sẽ vượt 5% khi SPY chạy xa
            # MA200 → báo oan mục nặng). Cả 2 đều là match văn xuôi.
            _after = txt[m.end():m.end() + 2]
            if _after[:1] in ("B", "M"):
                continue  # $XB/$XM = số tiền tỷ/triệu, không phải giá
            if re.match(r"\s*\(đóng\s+\d{1,2}/\d{1,2}",
                        txt[m.end():m.end() + 20], re.I):
                continue  # giá trị có ngày đóng cửa ngay sau số
            tail = txt[m.end():m.end() + 6]
            if tail.lstrip().lower().startswith("tỷ"):
                continue  # doanh thu $X tỷ — không phải giá
            if re.match(r"\s*·\s*(?:DT\s*)?\$[\d.,]+[BM]\b",
                        txt[m.end():m.end() + 20]):
                continue  # EPS kỳ vọng trong list earnings
                # (TICKER ($x · $yB/$yM) hoặc ($x · DT $yB)) — số trước là
                # EPS, không phải giá (vụ 05/11: sec-plan 13 mã lệch 96–99%)
            if re.match(r"\s*[–\-]\s*\$?[\d.,]+", txt[m.end():m.end() + 14]) \
                    and "giá" not in m.group(2).lower()[-8:]:
                continue  # đầu một KHOẢNG ($145.5–147.5 = kệ/vùng dịch
                # của xe...) — là MỐC, không phải giá hiện tại; riêng
                # khuôn vùng hàng bảng đã có kiểm span.zone ở dưới.
            zn = zone_numbers(zones.get(t))
            is_zone = any(abs(val - zv) <= max(0.75, abs(zv) * 0.006)
                          for zv in zn)
            if re.match(r"\s*\([−+\-]?\s*[\d.,]+%",
                        txt[m.end():m.end() + 12]):
                is_zone = False  # số kèm (+x%) luôn là GIÁ, kể cả khi
                # tình cờ sát mốc vùng (AMD $511,29 vs mép $512)
            if is_zone:
                continue  # số vùng khớp zones.json -> đúng
            # Số VÙNG = từ khóa vùng đứng NGAY trước $ (Vào $ / Chốt $ /
            # trong $ / Cắt <$...). "Giá $X" là GIÁ dù đầu câu có chữ VÙNG
            # của nhãn trạng thái (lỗi phân loại bản đầu: chip "TRÊN VÙNG ·
            # Giá $260" bị tính là số vùng).
            tail = re.sub(r"[\s·•|]+$", "", mid.lower())[-16:]
            zone_tail = re.search(
                r"(vùng|vào|chốt|cắt|target|trong|trên|dưới|mép|sát|tới|"
                r"còn trên|theo vùng)\s*<?$", tail)
            if zone_tail and zn:
                # ghi số kiểu vùng nhưng KHÔNG khớp zones.json -> vùng cũ
                zbad.setdefault(t, []).append(val)
                continue
            live = float(prices[t][0])
            diff = abs(val - live) / live * 100 if live else 0
            old = checked.get(t)
            if old is None or diff > old[1]:
                checked[t] = (val, diff)
        # Hàng bảng: kiểm TRỰC TIẾP khuôn "Vào $a–$b • Chốt $c→$d • Cắt
        # dưới $e" trong span.zone của hàng so với zones.json (bắt vụ MU
        # swing còn vùng cũ $890–920/cắt $870 sau khi đã vẽ lại).
        rowpat = re.compile(r'<tr><td><b>([A-Z]{2,6})</b></td>(.*?)</tr>',
                            re.S)
        zpat = re.compile(
            r"Vào\s*\$([\d.,]+)[–\-]\$?([\d.,]+)\s*•\s*Chốt\s*\$([\d.,]+)"
            r"(?:→\$?([\d.,]+))?\s*•\s*(?:<[^>]+>\s*)*"
            r"Cắt(?:\s*dưới)?\s*<?\$([\d.,]+)")
        for rm in rowpat.finditer(seg):
            t = rm.group(1)
            z = zones.get(t)
            if not z or not z.get("entry"):
                continue
            zm = zpat.search(rm.group(2))
            if not zm:
                continue
            got = [parse_num(x) for x in zm.groups() if x]
            want = [float(z["entry"][0]), float(z["entry"][1])]
            want += [float(x) for x in (z.get("targets") or [])]
            if z.get("cut"):
                want.append(float(z["cut"]))
            if len(got) != len(want) or any(
                    abs(g - w) > max(0.75, abs(w) * 0.006)
                    for g, w in zip(got, want)):
                zbad.setdefault(t, []).append(
                    {"printed_zone": got, "zones_json": want})
        bad = sorted(
            ({"t": t, "printed": v, "live": float(prices[t][0]),
              "diff_pct": round(d, 2)}
             for t, (v, d) in checked.items() if d > 1.0),
            key=lambda x: -x["diff_pct"])
        report[sid] = {
            "checked": len(checked),
            "bad": bad,
            "zone_bad": {t: v for t, v in zbad.items()},
        }
    return report


HEAVY_SECS = ("sec-daytrade", "sec-swing", "sec-longterm", "sec-so2",
              "sec-so3", "sec-leverage", "sec-special", "sec-topfocus",
              "sec-plan", "sec-hot", "sec-watch", "sec-steelnet",
              "sec-etf", "sec-etf16", "sec-ltalloc", "sec-lt9",
              "sec-dram")


def heavy(report):
    """Mục LỆCH NẶNG: ≥3 mã lệch >1% hoặc 1 mã lệch >5% hoặc vùng sai.
    (sec-news/sec-urgent/sec-ahshark chỉ tham khảo: headline chứa PT/số
    tin, alert có timestamp riêng — không tính nặng.)"""
    out = []
    for sid, r in report.items():
        if sid not in HEAVY_SECS:
            continue
        if len(r["bad"]) >= 3 or any(b["diff_pct"] > 5 for b in r["bad"]) \
                or r["zone_bad"]:
            out.append(sid)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=LIVE_BASE + "/")
    ap.add_argument("--prices", default=LIVE_BASE + "/prices.json")
    ap.add_argument("--zones", default=os.path.join(HERE, "zones.json"))
    ap.add_argument("--out", default="")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    html_src, px_src = a.html, a.prices
    try:
        html = fetch(a.html)
    except Exception:
        html_src = os.path.join(HERE, "index.html")
        html = fetch(html_src)
    try:
        pj = json.loads(fetch(a.prices))
    except Exception:
        px_src = os.path.join(HERE, "prices.json")
        pj = json.loads(fetch(px_src))
    prices = pj.get("q") or {}
    zones = json.load(open(a.zones, encoding="utf-8"))
    rep = audit(html, prices, zones)
    hv = heavy(rep)
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    doc = {"ts": now.strftime("%Y-%m-%d %H:%M:%S PT"),
           "html_src": html_src, "prices_src": px_src,
           "prices_ts": pj.get("ts"), "heavy_secs": hv, "sections": rep}
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    tot_bad = sum(len(r["bad"]) for r in rep.values())
    if a.quiet:
        print(f"AUDIT {doc['ts']}: lệch>1%={tot_bad} mã-mục"
              f" · mục nặng: {', '.join(hv) or 'không'}")
    else:
        print(f"=== AUDIT SỐ LIỆU — {doc['ts']} ===")
        print(f"HTML: {html_src} · prices: {px_src} (ts {pj.get('ts')})")
        for sid in TARGET_SECS:
            r = rep.get(sid)
            if not r:
                continue
            line = (f"{sid}: kiểm {r['checked']} mã · "
                    f"lệch>1%: {len(r['bad'])}")
            if r["zone_bad"]:
                line += f" · VÙNG SAI: {r['zone_bad']}"
            print(line)
            for b in r["bad"][:8]:
                print(f"   {b['t']}: in ${b['printed']} vs live "
                      f"${b['live']} ({b['diff_pct']}%)")
        print(f"TỔNG lệch>1%: {tot_bad} · MỤC NẶNG: {hv or 'không'}")
    return 1 if hv else 0


if __name__ == "__main__":
    sys.exit(main())
