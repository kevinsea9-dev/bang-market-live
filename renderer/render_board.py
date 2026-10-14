#!/usr/bin/env python3
"""Render Bảng Market Live HTML from board-data.json.

Usage:
    python3 render_board.py [input.json] [output.html]

Defaults: input ../board-data.json, output test-output.html (same dir as this script).

Design: workers write ONLY the JSON (numbers, text, statuses). This script owns
ALL HTML structure, so a worker can never break tags, lose the PIN gate, or drop
the share button again.

Conventions for board-data.json:
  - fields ending in _html contain raw HTML (trusted, written by the scan worker).
  - plain-text fields (label, title, time, ...) are emitted verbatim; the worker
    must HTML-escape them (& -> &amp; etc.) before writing the JSON.
  - tone: "hot" | "good" | "plain"  -> alert CSS class suffix.
  - status: "in" | "near" | "out" | "cut" -> badge class b-in/b-near/b-out/b-cut.
  - spark_pct: number (day % change) or null -> momentum sparkline SVG, or none.
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def chunk(name):
    with open(os.path.join(BASE, 'static', name), encoding='utf-8') as f:
        return f.read()


CHUNK_HEAD = chunk('chunk_head.html')          # doctype -> <span class="updated">
CHUNK_MID = chunk('chunk_mid.html')            # </span> -> end of tabbar (share btns, PIN JS, tabs)
CHUNK_FOOTER_TOP = chunk('chunk_footer_top.html')
CHUNK_FOOTER_BOTTOM = chunk('chunk_footer_bottom.html')

TONE_CLASS = {'hot': ' hot', 'good': ' good', 'plain': ''}
STATUS_CLASS = {'in': 'b-in', 'near': 'b-near', 'out': 'b-out', 'cut': 'b-cut'}
ACCENT_STYLE = {'red': ' style="border-color:#7a2c2c"',
                'green': ' style="border-color:#2c7a4b"',
                'none': ''}


def spark_svg(pct):
    """Momentum sparkline from day % change. Y = 11 - pct*0.8, clamped [2,20]."""
    y = 11 + round(-pct * 0.8)
    y = max(2, min(20, y))
    c = '#f87171' if pct < 0 else '#4ade80'
    return (' <svg class="spark" width="70" height="22" viewBox="0 0 70 22">'
            '<line x1="2" y1="11" x2="68" y2="11" stroke="#3a4a63" stroke-width="1"/>'
            f'<polyline points="4,11 66,{y}" fill="none" stroke="{c}" stroke-width="2.5" stroke-linecap="round"/>'
            f'<circle cx="66" cy="{y}" r="3" fill="{c}"/></svg>')


def alert_div(a, with_time=True):
    cls = TONE_CLASS.get(a.get('tone', 'plain'), '')
    s = f'  <div class="alert{cls}">{a["html"]}'
    if with_time and a.get('time'):
        s += f'<br><span class="t">{a["time"]}</span>'
    return s + '</div>'


def sec_market(d):
    cards = '\n'.join(
        f'    <div class="card"><div class="lbl">{c["label"]}</div>'
        f'<div class="val {c["dir"]}">{c["val_html"]}</div></div>'
        for c in d['cards'])
    return ('<div class="sec">\n'
            f'  <h2>{d["title"]}</h2>\n'
            f'  <div class="grid">\n{cards}\n  </div>\n'
            f'  <div class="note">{d["note_html"]}</div>\n'
            '</div>')


def sec_zone_signals(d):
    alerts = '\n'.join(alert_div(a) for a in d['alerts'])
    return ('<div class="sec" id="sec-swing">\n'
            '  <h2>🚨 TÍN HIỆU VÙNG — mới nhất lên đầu</h2>\n'
            f'{alerts}\n'
            '</div>')


def sec_top_attention(d):
    alerts = '\n'.join(alert_div(a, with_time=False) for a in d['alerts'])
    return ('<div class="sec">\n'
            f'  <h2>🔔 ĐÁNG CHÚ Ý NHẤT — 4 mã cần nhìn đầu tiên ({d["title_suffix"]})</h2>\n'
            f'{alerts}\n'
            f'  <div class="note">{d["note"]}</div>\n'
            '</div>')


def sec_hunt2(d):
    rows = '\n'.join(
        f'    <tr><td><b>{r["ticker"]}</b></td><td>{r["price_html"]}</td>'
        f'<td>{r["zone"]}</td>'
        f'<td><span class="badge b{r["book"]}">Sổ {r["book"]}</span></td></tr>'
        for r in d['rows'])
    return ('<div class="sec">\n'
            f'  <h2>{d["title"]}</h2>\n'
            f'  <div class="note" style="margin-bottom:8px">{d["intro_html"]}</div>\n'
            '  <table>\n'
            f'    {d["table_head"]}\n'
            f'{rows}\n'
            '  </table>\n'
            f'  <div class="note">{d["note"]}</div>\n'
            '</div>')


def sec_sector_map(d):
    cards = '\n'.join(
        f'    <div class="card"{ACCENT_STYLE[c["accent"]]}>'
        f'<div class="lbl">{c["icon"]} {c["name"]}</div>'
        f'<div class="val {c["dir"]}" style="font-size:14px">{c["val_html"]}</div>'
        f'<div class="note">{c["note"]}</div></div>'
        for c in d['cards'])
    return ('<div class="sec">\n'
            f'  <h2>🗺️ BẢN ĐỒ SECTOR — tiền đang chạy đi đâu ({d["title_suffix"]})</h2>\n'
            f'  <div class="grid" style="grid-template-columns:1fr 1fr">\n{cards}\n  </div>\n'
            f'  <div class="note">{d["note"]}</div>\n'
            '</div>')


def sec_market_pulse(d):
    return ('<div class="sec">\n'
            f'  <h2>⚡ NHỊP THỊ TRƯỜNG — ai nóng ai lạnh ({d["title_suffix"]})</h2>\n'
            '  <div class="grid" style="grid-template-columns:1fr 1fr">\n'
            '    <div class="card" style="border-color:#2c7a4b"><div class="lbl">🔥 NÓNG NHẤT</div>'
            f'<div class="val up" style="font-size:14px;line-height:1.9">{d["hot_html"]}</div></div>\n'
            '    <div class="card" style="border-color:#7a2c2c"><div class="lbl">❄️ LẠNH NHẤT</div>'
            f'<div class="val down" style="font-size:14px;line-height:1.9">{d["cold_html"]}</div></div>\n'
            '  </div>\n'
            f'  <div class="note">{d["note"]}</div>\n'
            '</div>')


def sec_hot_tickers(d):
    parts = []
    for a in d['alerts']:
        cls = TONE_CLASS.get(a.get('tone', 'plain'), '')
        spark = spark_svg(a['spark_pct']) if a.get('spark_pct') is not None else ''
        parts.append(
            f'  <div class="alert{cls}"><b>{a["head"]}</b>{spark} — {a["body"]}'
            f'<br><span class="t">{a["time"]}</span></div>')
    return ('<div class="sec" id="sec-daytrade">\n'
            f'  <h2>🔥 MÃ NÓNG HÔM NAY — mỗi mã một vị ({d["title_suffix"]})</h2>\n'
            + '\n'.join(parts) + '\n'
            '</div>')


def sec_options(d):
    return ('<div class="sec">\n'
            '  <h2>🎰 KÈO OPTIONS TRONG NGÀY — Sổ 3, tiền mất được</h2>\n'
            f'  <div class="alert">{d["main_html"]}<br><span class="t">{d["main_time"]}</span></div>\n'
            f'  <div class="note">{d["note_html"]}</div>\n'
            '</div>')


def sec_night_owl(d):
    parts = []
    for a in d['alerts']:
        cls = TONE_CLASS.get(a.get('tone', 'plain'), '')
        parts.append(
            f'  <div class="alert{cls}"><b>{a["title"]}</b> {a["body"]}'
            f'<br><span class="t">{a["time"]}</span></div>')
    return ('<div class="sec">\n'
            '  <h2>🌙 CÚ ĐÊM — OVERNIGHT / CHÂU Á / CHÂU ÂU</h2>\n'
            + '\n'.join(parts) + '\n'
            f'  <div class="note">{d["note"]}</div>\n'
            '</div>')


BOOK_TITLES = {
    'book2': '📗 SỔ 2 — SWING (20%)',
    'book1': '📘 SỔ 1 — DÀI HẠN (70%)',
    'book3': '📒 SỔ 3 — ĐẦU CƠ (10%)',
}
BOOK_IDS = {'book2': '', 'book1': ' id="sec-longterm"', 'book3': ''}


def sec_book(key, d):
    rows = []
    for r in d['rows']:
        new = ' <span class="badge b-new">MỚI</span>' if r.get('is_new') else ''
        rows.append(
            f'    <tr><td><b>{r["ticker"]}</b></td><td>{r["price_html"]}</td>'
            f'<td><span class="zone">{r["zone_html"]}</span></td>'
            f'<td><span class="badge {STATUS_CLASS[r["status"]]}">{r["status_label"]}</span>{new}</td></tr>')
    return ('<div class="sec"' + BOOK_IDS[key] + '>\n'
            f'  <h2>{BOOK_TITLES[key]}</h2>\n'
            '  <table>\n'
            f'    {d["table_head"]}\n'
            + '\n'.join(rows) + '\n'
            '  </table>\n'
            '</div>')


def sec_big_news(d):
    items = '\n'.join(f'    {it}' for it in d['items'])
    return ('<div class="sec">\n'
            f'  <h2>📰 TIN LỚN ({d["title_suffix"]})</h2>\n'
            '  <ul>\n'
            f'{items}\n'
            '  </ul>\n'
            '</div>')


def sec_calendar(d):
    rows = '\n'.join(
        f'  <div class="cal"><div class="d">{r["day"]}</div><div>{r["html"]}</div></div>'
        for r in d['rows'])
    return ('<div class="sec">\n'
            '  <h2>📅 LỊCH TUẦN NÀY</h2>\n'
            f'{rows}\n'
            '</div>')


def render(data):
    sections = [
        sec_market(data['market']),
        sec_zone_signals(data['zone_signals']),
        sec_top_attention(data['top_attention']),
        sec_hunt2(data['hunt2']),
        sec_sector_map(data['sector_map']),
        sec_market_pulse(data['market_pulse']),
        sec_hot_tickers(data['hot_tickers']),
        sec_options(data['options']),
        sec_night_owl(data['night_owl']),
        sec_book('book2', data['book2']),
        sec_book('book1', data['book1']),
        sec_book('book3', data['book3']),
        sec_big_news(data['big_news']),
        sec_calendar(data['calendar']),
    ]
    return (CHUNK_HEAD + data['meta']['badge'] + CHUNK_MID
            + '\n\n' + '\n\n'.join(sections)
            + '\n\n' + CHUNK_FOOTER_TOP + data['meta']['footer_note'] + CHUNK_FOOTER_BOTTOM)


def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, '..', 'board-data.json')
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BASE, 'test-output.html')
    with open(inp, encoding='utf-8') as f:
        data = json.load(f)
    html = render(data)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'rendered {len(html)} bytes -> {out}')


if __name__ == '__main__':
    main()
