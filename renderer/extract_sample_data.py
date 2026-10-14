#!/usr/bin/env python3
"""Parse current bang-market-live.html into board-data.json (sample data).
Run: python3 extract_sample_data.py
Output: ~/workspace/market-board/board-data.json
"""
import re, json, os

SRC = '/home/hatch/workspace/goals/watchlist-l-t-s-ng-h-ng-ng-y/files/bang-market-live.html'
OUT = '/home/hatch/workspace/market-board/board-data.json'
html = open(SRC, encoding='utf-8').read()

# ---- section slicing ----
anchors = [
    ('market', '<h2>🌍'),
    ('zone', 'id="sec-swing"'),
    ('top', '<h2>🔔'),
    ('hunt2', '<h2>🎯'),
    ('sector', '<h2>🗺️'),
    ('pulse', '<h2>⚡'),
    ('hot', 'id="sec-daytrade"'),
    ('options', '<h2>🎰'),
    ('night', '<h2>🌙'),
    ('book2', '<h2>📗'),
    ('book1', 'id="sec-longterm"'),
    ('book3', '<h2>📒'),
    ('news', '<h2>📰'),
    ('cal', '<h2>📅'),
    ('footer', '<div class="footer">'),
]
pos = {}
for name, a in anchors:
    pos[name] = html.index(a)
order = [n for n, _ in anchors]
sec = {}
for k in range(len(order)):
    start = pos[order[k]]
    end = pos[order[k + 1]] if k + 1 < len(order) else len(html)
    sec[order[k]] = html[start:end]

def alerts_in(s):
    """Parse <div class="alert..."> blocks. Returns list of dicts."""
    out = []
    for m in re.finditer(r'<div class="alert(.*?)">(.*?)</div>', s, re.S):
        tone_raw, inner = m.group(1), m.group(2)
        tone = 'hot' if 'hot' in tone_raw else ('good' if 'good' in tone_raw else 'plain')
        tm = re.search(r'<br><span class="t">(.*?)</span>$', inner, re.S)
        if tm:
            time = tm.group(1)
            body = inner[:tm.start()]
        else:
            time, body = None, inner
        out.append({'tone': tone, 'html': body, 'time': time})
    return out

data = {}

# meta
data['meta'] = {
    'badge': re.search(r'<span class="updated">(.*?)</span>', html, re.S).group(1),
    'footer_note': re.search(r'<div class="footer">.*?<span class="note">(.*?)</span>', html, re.S).group(1),
}

# market
m = sec['market']
data['market'] = {
    'title': re.search(r'<h2>(.*?)</h2>', m, re.S).group(1),
    'cards': [
        {'label': c[0], 'dir': c[1], 'val_html': c[2]}
        for c in re.findall(r'<div class="card"><div class="lbl">(.*?)</div><div class="val (.*?)">(.*?)</div></div>', m, re.S)
    ],
    'note_html': re.findall(r'<div class="note">(.*?)</div>', m, re.S)[-1],
}

# zone signals
data['zone_signals'] = {'alerts': alerts_in(sec['zone'])}

# top attention
t = sec['top']
title = re.search(r'<h2>(.*?)</h2>', t, re.S).group(1)
suffix = re.search(r'\((.*)\)\s*$', title).group(1)
notes = re.findall(r'<div class="note">(.*?)</div>', t, re.S)
data['top_attention'] = {'title_suffix': suffix, 'alerts': alerts_in(t), 'note': notes[-1]}

# hunt2
h = sec['hunt2']
data['hunt2'] = {
    'title': re.search(r'<h2>(.*?)</h2>', h, re.S).group(1),
    'intro_html': re.search(r'<div class="note" style="margin-bottom:8px">(.*?)</div>', h, re.S).group(1),
    'table_head': re.search(r'<tr><th>.*?</tr>', h, re.S).group(0),
    'rows': [
        {'ticker': r[0], 'price_html': r[1], 'zone': r[2], 'book': int(r[4])}
        for r in re.findall(r'<tr><td><b>(.*?)</b></td><td>(.*?)</td><td>(.*?)</td><td><span class="badge (b\d)">Sổ (\d)</span></td></tr>', h, re.S)
    ],
    'note': re.findall(r'<div class="note">(.*?)</div>', h, re.S)[-1],
}

# sector map
s = sec['sector']
stitle = re.search(r'<h2>(.*?)</h2>', s, re.S).group(1)
cards = []
for cm in re.finditer(r'<div class="card"(.*?)><div class="lbl">(.*?)</div><div class="val (.*?)" style="font-size:14px">(.*?)</div><div class="note">(.*?)</div></div>', s, re.S):
    style, lbl, d, val, note = cm.groups()
    accent = 'red' if '#7a2c2c' in style else ('green' if '#2c7a4b' in style else 'none')
    icon, name = lbl.split(' ', 1)
    cards.append({'icon': icon, 'name': name, 'accent': accent, 'dir': d, 'val_html': val, 'note': note})
data['sector_map'] = {
    'title_suffix': re.search(r'\((.*)\)\s*$', stitle).group(1),
    'cards': cards,
    'note': re.findall(r'<div class="note">(.*?)</div>', s, re.S)[-1],
}

# market pulse
p = sec['pulse']
ptitle = re.search(r'<h2>(.*?)</h2>', p, re.S).group(1)
hot_html = re.search(r'NÓNG NHẤT</div><div class="val up" style="font-size:14px;line-height:1.9">(.*?)</div>', p, re.S).group(1)
cold_html = re.search(r'LẠNH NHẤT</div><div class="val down" style="font-size:14px;line-height:1.9">(.*?)</div>', p, re.S).group(1)
data['market_pulse'] = {
    'title_suffix': re.search(r'\((.*)\)\s*$', ptitle).group(1),
    'hot_html': hot_html, 'cold_html': cold_html,
    'note': re.findall(r'<div class="note">(.*?)</div>', p, re.S)[-1],
}

# hot tickers
SPARK_PCT = {
    'AMAT ~$465': -6.5, 'WDC ~−6%': -6.0, 'SNDK ~−4.3%': -4.3, 'MU ~$989': -4.5,
    'SK Hynix (Seoul) −10%': -10.0, 'NVDA $228.52': 1.0, 'TSLA ~$375': -0.74,
    'SPCX $156.54': -4.2, 'AAPL ~$344.22': 0.95, 'TSM $445.12': 0.17,
    'BA $186.49': -1.6, 'AMD $605.53': 1.9, 'DELL $572.47': -0.7,
    'AVGO $355.61': 0.3, 'ASML báo cáo Q3 (14/10 sáng)': 5.0,
}
ht = sec['hot']
htitle = re.search(r'<h2>(.*?)</h2>', ht, re.S).group(1)
hot_alerts = []
for a in alerts_in(ht):
    inner = a['html']
    bm = re.match(r'<b>(.*?)</b>(.*)', inner, re.S)
    head, rest = bm.group(1), bm.group(2)
    has_spark = '<svg class="spark"' in rest
    pct = None
    if has_spark:
        for k, v in SPARK_PCT.items():
            if head.startswith(k):
                pct = v
                break
        assert pct is not None, f'no pct for spark head: {head}'
        rest = re.sub(r' <svg class="spark".*?</svg>', '', rest, count=1, flags=re.S)
    assert rest.startswith(' — '), f'bad rest: {rest[:40]}'
    body = rest[3:]
    hot_alerts.append({'tone': a['tone'], 'head': head, 'spark_pct': pct, 'body': body, 'time': a['time']})
data['hot_tickers'] = {
    'title_suffix': re.search(r'\((.*)\)\s*$', htitle).group(1),
    'alerts': hot_alerts,
}

# options
o = sec['options']
oalerts = alerts_in(o)
onotes = re.findall(r'<div class="note">(.*?)</div>', o, re.S)
data['options'] = {'main_html': oalerts[0]['html'], 'main_time': oalerts[0]['time'], 'note_html': onotes[0]}

# night owl
n = sec['night']
nalerts = []
for a in alerts_in(n):
    bm = re.match(r'<b>(.*?)</b>(.*)', a['html'], re.S)
    title_b, rest = bm.group(1), bm.group(2)
    assert rest.startswith(' '), f'bad night rest: {rest[:40]}'
    nalerts.append({'tone': a['tone'], 'title': title_b, 'body': rest[1:], 'time': a['time']})
nnotes = re.findall(r'<div class="note">(.*?)</div>', n, re.S)
data['night_owl'] = {'alerts': nalerts, 'note': nnotes[-1]}

# books
def book_rows(s):
    rows = []
    for r in re.findall(r'<tr><td><b>(.*?)</b></td><td>(.*?)</td><td><span class="zone">(.*?)</span></td><td>(.*?)</td></tr>', s, re.S):
        ticker, price, zone, badges = r
        bm = re.findall(r'<span class="badge (.*?)">(.*?)</span>', badges)
        cls, label = bm[0]
        status = {'b-in': 'in', 'b-near': 'near', 'b-out': 'out', 'b-cut': 'cut'}[cls]
        is_new = any(c == 'b-new' for c, _ in bm[1:])
        rows.append({'ticker': ticker, 'price_html': price, 'zone_html': zone,
                     'status': status, 'status_label': label, 'is_new': is_new})
    return rows

data['book2'] = {'table_head': re.search(r'<tr><th>.*?</tr>', sec['book2'], re.S).group(0),
                 'rows': book_rows(sec['book2'])}
data['book1'] = {'table_head': re.search(r'<tr><th>.*?</tr>', sec['book1'], re.S).group(0),
                 'rows': book_rows(sec['book1'])}
data['book3'] = {'table_head': re.search(r'<tr><th>.*?</tr>', sec['book3'], re.S).group(0),
                 'rows': book_rows(sec['book3'])}

# big news
nw = sec['news']
ntitle = re.search(r'<h2>(.*?)</h2>', nw, re.S).group(1)
data['big_news'] = {
    'title_suffix': re.search(r'\((.*)\)\s*$', ntitle).group(1),
    'items': ['<li>' + i + '</li>' for i in re.findall(r'<li>(.*?)</li>', nw, re.S)],
}

# calendar
c = sec['cal']
data['calendar'] = {
    'rows': [{'day': r[0], 'html': r[1]}
             for r in re.findall(r'<div class="cal"><div class="d">(.*?)</div><div>(.*?)</div></div>', c, re.S)]
}

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print('wrote', OUT, os.path.getsize(OUT), 'bytes')
print('counts:', {k: (len(v['alerts']) if 'alerts' in v else len(v.get('rows', v.get('items', v.get('cards', []))))) for k, v in data.items() if isinstance(v, dict)})
