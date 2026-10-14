# MIGRATION: Chuyển 10 cron sang flow JSON → render → push

Ngày: 14/10/2026. Người duyệt: Tom. Trạng thái: renderer đã build + test xong, **chưa migration**.

## 1. Đã build xong (trong `~/workspace/market-board/renderer/`)

| File | Vai trò |
|---|---|
| `render_board.py` | Đọc `../board-data.json` → render HTML hoàn chỉnh. Worker không bao giờ đụng HTML nữa. |
| `static/chunk_*.html` | 4 khối tĩnh trích nguyên văn từ bảng live: CSS+PIN gate+header (head), nút share/script/tabbar (mid), footer (top/bottom). Chứa PIN gate, `shareBoard()`, eye toggle, `BOARD_PIN`, toàn bộ CSS, tabbar anchors, nút ⬆️. |
| `extract_sample_data.py` | Parse bảng live hiện tại → JSON mẫu (dùng 1 lần, giữ lại để tái tạo mẫu sau này). |
| `test-output.html` | Output test — **byte-identical** với bảng live hiện tại (diff = 0). |
| `../board-data.json` | JSON mẫu với dữ liệu thật 14/10 05:37 PT (19 tín hiệu vùng, 19 mã nóng, 15 hunt2, 7+11+6 hàng 3 sổ, 25 tin, 5 lịch...). |

Test đã pass: byte-identical, thẻ cân bằng (div/table/tr/td/span/script/a...), render từ JSON đột biến (section rỗng, spark null, pct cực trị) vẫn ra HTML hợp lệ, PIN gate + share luôn còn nguyên.

## 2. Flow mới cho mỗi cron (thay đoạn "viết lại HTML bằng tay")

```
1. Worker quét tin/giá như cũ, nhưng GHI KẾT QUẢ vào board-data.json
   (ghi đè file ~/workspace/market-board/board-data.json — schema trong render_board.py)
2. Chạy:  cd ~/workspace/market-board/renderer && python3 render_board.py
   → sinh index.html mới (ghi đè ~/workspace/market-board/index.html)
3. Validate nhanh (bắt buộc): python3 -c "đếm thẻ div/table/script cân bằng; assert 'PIN-GATE-START' in html"
   → fail thì DỪNG, không push, ghi run log.
4. git add + commit + push như cũ (kèm retry 3 lần nếu push lỗi — xem task 1/3).
```

Worker **không được** sửa `renderer/static/*`, không đụng HTML trực tiếp.

## 3. Thứ tự migration (làm dần, không "big bang")

1. **Pilot (1 cron ít rủi ro):** `overnight-asia-scan` (20:37, ít người xem). Chạy song song 2-3 đêm: worker vừa viết JSON mới, vừa giữ cách cũ → so sánh output.
2. **Verify pilot:** diff HTML render vs HTML viết tay — chỉ khác nội dung động thì đạt.
3. **Mở rộng:** 2 cron Cú Đêm còn lại → 7 cron ngày, mỗi đợt cách nhau 1 ngày để kịp phát hiện lỗi.
4. **Dọn dẹp:** khi cả 10 đã chạy ổn 3 ngày, rút gọn body cron (bỏ đoạn guard chống đè + bảo vệ PIN gate — renderer đã bảo đảm bằng cấu trúc, không cần dặn bằng lời nữa). Giữ lại file HTML cũ `files/bang-market-live.html` làm bản dự phòng.

Mỗi bước dùng `cron.view` → `cron.update` (không sửa file .md).

## 4. Rủi ro & cách xử lý

| Rủi ro | Xử lý |
|---|---|
| Worker viết JSON sai schema (thiếu key) | `render_board.py` crash loud → validate fail → không push bảng hỏng. Thêm JSON schema check (khuyến nghị) trước render. |
| 2 worker ghi JSON cùng lúc (race) | Timestamp guard chuyển vào wrapper: chỉ render khi JSON mới hơn bản đã push (so `meta.badge`). |
| Muốn đổi PIN / thêm section mới | Sửa `static/` + `render_board.py` (việc của main agent, có test byte-diff), không phải việc của worker quét. |
| Muốn rollback | `git log` trong `~/workspace/market-board` còn đủ lịch sử; file `files/bang-market-live.html` vẫn là bản tay cuối cùng — cron có thể quay lại flow cũ trong 1 lần `cron.update`. |

## 5. Schema JSON (tóm tắt — chi tiết trong docstring `render_board.py`)

- `meta`: badge text, footer_note
- `market`: title, cards[{label, dir: up|down|flat, val_html}], note_html
- `zone_signals` / `top_attention` / `night_owl`: alerts[{tone: hot|good|plain, html, time|null}]
- `hot_tickers`: alerts[{tone, head, spark_pct: number|null, body, time}] — sparkline tự vẽ từ %
- `hunt2`: title, intro_html, table_head, rows[{ticker, price_html, zone, book: 2|3}], note
- `sector_map`: cards[{icon, name, accent: red|green|none, dir, val_html, note}], note
- `market_pulse`: hot_html, cold_html, note
- `options`: main_html, main_time, note_html
- `book1/2/3`: table_head, rows[{ticker, price_html, zone_html, status: in|near|out|cut, status_label, is_new}]
- `big_news`: items[] (nguyên `<li>...</li>`), `calendar`: rows[{day, html}]

Quy ước: field `*_html` = HTML thô (worker tự chịu trách nhiệm escape); field text thường phải escape sẵn (`&` → `&amp;`).
