# Báo cáo cá nhân — Day 6

> Học viên: [Họ và tên] · MSSV: 2A202603878 · Lớp: L3B
> Các ô `[điền …]` lấy từ `starter/results/run_config_dev.json` / `run_config_test.json`.

## Dữ liệu và pipeline

- **Chủ đề, nguồn/quyền, số trang và quy tắc tính trang:** Báo cáo *Commodity Markets Outlook, October 2026* (World Bank, Prospects Group), tải từ World Bank Open Knowledge Repository. Giấy phép **CC BY 3.0 IGO** (PDF trang 4), cho phép sao chép, xử lý và phân phối lại nếu ghi nguồn. PDF có 70 trang vật lý; **tính 58 trang nội dung (PDF trang 9–66)**. Loại trang 1–8 (bìa, trang trống, bản quyền, mục lục, lời cảm ơn), 67–69 (References), 70 (bìa sau). `gold_page` và metadata `page` dùng **số trang PDF** (số in trên trang = trang PDF − 8). Chi tiết từng trang, sha256 và ngày chốt số liệu (14/10/2026) có trong `data/manifest.json`.
- **Chuẩn hóa, lỗi và giới hạn:** Đọc bằng `pypdf` (kèm `fonttools`), không cần OCR vì PDF có text layer. `normalize_text`: chuẩn hóa NFKC, bỏ ký tự điều khiển và soft hyphen, nối các dòng bị ngắt giữa câu (dòng kết thúc bằng dấu cách → soft wrap), giữ `\n` ở ranh giới đoạn/heading. Tiêu đề đầu trang được tách thành `section` + `printed_page` rồi xóa khỏi text (khớp 58/58 trang). Log `results/ingest_log.json`: 10 trang bị loại theo manifest, 2 trang rỗng (2, 8). Giới hạn đã quan sát:
  1. **Thứ tự text bị trộn** do bố cục 2 cột, hộp tóm tắt và chú thích hình (ví dụ trang 22: đoạn tóm tắt chương chen giữa cụm "memorandum of … understanding").
  2. **Số liệu biểu đồ bị trích thành text**: khoảng 2.070/2.697 dòng ngắn là nhãn trục. Được lọc bằng heuristic `is_noise_line` (bỏ dòng < 4 từ có chữ số, hoặc > 50% token là số), giữ 1.255 dòng. Chú giải dạng chữ ("Brent oil") vẫn lọt.
  3. Heuristic tách đoạn bỏ sót một số ranh giới; không bỏ gạch nối cuối dòng (tránh nối sai từ ghép); `pypdf` đôi khi tách chữ ("T otal").
  4. Bảng dự báo giá (trang 20–21) trích xuất lộn xộn và xuất hiện như nhiễu trong top-3 của q05, q18.
- **Embedding, vector store, metric, lệnh chạy và chi phí:** OpenAI `text-embedding-3-small`, 1536 chiều, SDK `openai` [điền phiên bản]. Query và document dùng cùng model, không prefix, gửi theo batch 100. Vector store: **PostgreSQL 16.12 + pgvector 0.8.1** (Docker image `pgvector/pgvector:0.8.7-pg16`), một bảng `chunks`, khóa `(strategy, chunk_id)`, **cosine distance `<=>`, exact search** (không dùng index ANN; khoảng 1.200 bản ghi). Lệnh chạy (từ `starter/`): `python -m src.run --split dev`, sau đó `python -m src.run --split test`. Chi phí: khoảng **104.500 token mỗi lần chạy** (phần lớn là embed lại tài liệu), ước tính khoảng **$0,0021 mỗi lần, tổng khoảng $0,004** với đơn giá $0,02/1M token [kiểm tra lại trên trang giá OpenAI]. Smoke test thêm khoảng 2.600 token.
- **Metadata bắt buộc, trường lọc bổ sung, nguồn giá trị và lý do chọn:** Mỗi chunk có `document_id`, `source`, `page` (bắt buộc), cùng `printed_page`, `section`, `chunk_index`, `content_hash`, tham số chunk. **Trường lọc: `section`** (text, 6 giá trị: executive_summary, energy, agriculture, fertilizers, metals_and_minerals, special_focus). Giá trị lấy trực tiếp từ tiêu đề đầu trang của PDF và đối chiếu với manifest, không suy đoán. Lý do chọn: báo cáo chia theo ngành và nhiều ngành dùng cùng kiểu câu và số liệu ("index projected to rise X% in 2026"), nên phạm vi ngành là ràng buộc có ích. Không dùng `year`, vì cả báo cáo là một ấn bản và các năm chỉ là năm dự báo trong nội dung.

## Cấu hình và benchmark

- **A / B / C:**
  - A: `fixed_size`, 500 ký tự, overlap 50, không lọc (555 chunk).
  - B: `by_paragraph`, gom đoạn tối đa 1.000 ký tự, đoạn dài cắt theo câu, heading cuối chunk được chuyển sang chunk sau, không lọc (324 chunk).
  - C: **giống hệt B** (cùng chunk, cùng vector của B), **khác duy nhất** là lọc `section` trong SQL khi câu hỏi có filter (4/20 câu); câu không có filter chạy không lọc.
  - Bộ lọc dòng nhiễu dùng chung cho cả A, B, C. Lưu ý: A–B khác cả cách chia lẫn kích thước (500 so với trung vị khoảng 813 ký tự), đây là yếu tố gây nhiễu khi so A–B.
- **Benchmark:** 20 câu (`benchmark/questions.json`, v1.1), **15 dev / 5 test chia cố định trước khi chạy**. Gồm 6 câu trực tiếp, 7 diễn đạt lại, 3 có điều kiện, 4 có filter `section` (dev: q05 energy, q11 metals; test: q18 fertilizers, q19 metals); câu filter cố ý viết mập mờ về ngành. Mỗi câu có `expected_answer`, `gold_page`, `evidence_quote` nguyên văn và `alternative_evidence` khi Executive Summary nhắc lại cùng số liệu. Bằng chứng nằm ngoài section của filter được đánh dấu `outside_filter`, chỉ tính cho cấu hình không lọc. v1.1 thêm 2 bằng chứng như vậy cho q05, q18, **trước khi chạy bất kỳ cấu hình nào**. Kiểm chứng: `benchmark/verify_questions.py` xác nhận mọi evidence là chuỗi con của text trang tương ứng (và đúng section khi có filter): 0 lỗi, kiểm tra lại sau khi chuẩn hóa. Rủi ro rò rỉ: chỉ một tài liệu nên dev/test chung nguồn; trang 43 và 57 xuất hiện ở cả hai tập; q18 (test) dùng cùng mẫu câu "sector's price index" với q11 (dev).
- **Tiêu chí chọn chiến lược (chốt trước khi xem test, ghi trong `questions.json`):** (1) Evidence Hit@3 cao nhất trên dev → (2) Hit@1 → (3) grounding trung bình → (4) ít chunk/chi phí hơn.

## Kết quả

| Strategy | Split | Hit@1 (n/N) | Evidence Hit@3 (n/N) | Chunk count | Thời gian | Chi phí |
|---|---|---|---|---|---|---|
| A | dev | 9/15 | 11/15 | 555 | embed [điền] s | [điền] token |
| A | test | 2/5 | 3/5 | 555 | | |
| B | dev | 10/15 | 14/15 | 324 | embed [điền] s | [điền] token |
| B | test | 3/5 | 4/5 | 324 | | |
| C | dev | 12/15 | 15/15 | 324 | dùng lại vector B | 0 token embed thêm |
| C | test | 4/5 | 5/5 | 324 | | |

Đọc PDF khoảng 4 giây. Truy xuất và chấm [điền] giây. Tổng mỗi lần chạy khoảng 104.500 token, khoảng $0,0021.

| Strategy | Coherence trung bình /2 (n) | Metadata đủ và đúng (n/N) | Filter tuân thủ (test, n/N) | Grounding 0/1/2 (số câu) và trung bình /2 |
|---|---|---|---|---|
| A | 0,70 (10) — 0: 3, 1: 7 | 15/15 | 1/2 | 1 / 1 / 3 — 1,40 |
| B | 1,30 (10) — 1: 7, 2: 3 | 15/15 | 1/2 | 1 / 0 / 4 — 1,60 |
| C | 1,30 (10) — giống B | 15/15 | 2/2 | 0 / 0 / 5 — 2,00 |

**Đường dẫn trace, nhãn thủ công và cách chọn mẫu:**
- Trace top-3 đầy đủ: `starter/results/traces_dev.jsonl`, `traces_test.jsonl`. Chỉ số: `metrics_*.json`. Cấu hình, phiên bản, token: `run_config_*.json`. Thống kê chunk: `chunk_stats.json`.
- Nhãn: `starter/results/labels/`.
  - **Coherence:** 10 vị trí chọn trước khi xem điểm. Đó là 10 trang nội dung đầu tiên (9–18); mỗi trang lấy câu đầu tiên ≥ 80 ký tự nằm sau 40% độ dài văn bản. Chấm chunk chứa câu đó ở mỗi chiến lược.
  - **Metadata:** 5 câu test × top-3 × 3 chiến lược = 45 bản ghi, đối chiếu tự động với manifest và xác nhận text chunk nằm trên đúng trang.
  - **Grounding:** 5 câu test × 3 chiến lược, chấm top-3 gộp.
- **Phương pháp chấm:** nhãn coherence và grounding do Claude (LLM) chấm nháp theo thang 0–2 của GUIDE, kèm lý do từng dòng; tác giả duyệt lại và sửa [điền số] nhãn. Nguy cơ thiên lệch: cùng một trợ lý AI đã soạn benchmark, code và chấm nháp; tác giả cũng là người chấm.

## Phân tích và kết luận

- **A so với B (đổi cách chia chunk):** B tăng Evidence Hit@3 từ 11/15 lên 14/15 (dev) và từ 3/5 lên 4/5 (test); coherence từ 0,70 lên 1,30. Trong 6 lần A trượt, **5 lần top-3 đã có đúng trang gold** nhưng cửa sổ 500 ký tự cắt evidence qua 2 chunk (cờ `page_hit_no_evidence`: q07, q09, q11, q12, q19). Embedding tìm đúng vị trí; chiến lược chunk làm vỡ bằng chứng. Chỉ q17 (Box El Niño, trang 43) là trượt truy xuất thật.
  **B so với C (chỉ đổi filter):** với 16 câu không filter, B và C cho kết quả giống hệt về cấu trúc. Toàn bộ chênh lệch (Hit@3 dev 14 lên 15, test 4 lên 5; Hit@1 dev 10 lên 12) đến từ 4 câu filter. Filter tuân thủ: A và B 1/2, C 2/2 trên test.
  **Ví dụ sai 1, A–q12 (dev):** câu về rủi ro gia hạn force majeure của Qatar với giá urê, gold trang 48 (`wb-cmo-2026-10`). A lấy 2 chunk của trang 48 nhưng câu evidence bị cửa sổ cố định cắt đôi, nên không chunk nào chứa trọn → trượt. B giữ nguyên đoạn → trúng.
  **Ví dụ sai 2, B–q18 (test):** "How much is the sector's price index projected to rise in 2026?" (ngữ cảnh: phân bón, gold trang 47). B xếp **trang 34 (agriculture)** đứng đầu, nơi giá ngũ cốc cũng "+14% năm 2026": trùng số, trùng năm, sai ngành. Hai kết quả còn lại là bảng dự báo trang 21 và metals trang 15. C lọc `section=fertilizers` đưa trang 47 lên đầu (grounding 2).
- **Chiến lược chọn, đánh đổi, điều kiện áp dụng:** Chọn **C** theo tiêu chí đã chốt (Evidence Hit@3 dev 15/15). Test chạy ngay sau đó, không chỉnh sửa gì. Đánh đổi:
  - Chunk theo đoạn dài hơn (nhiều ngữ cảnh hơn cho mỗi token, nhưng kém chính xác về vị trí).
  - Filter chỉ có ích khi người dùng hoặc ngữ cảnh **cung cấp đúng section trước**. Filter cũng loại bỏ bằng chứng hợp lệ ở Executive Summary (ví dụ trang 14 cho q18). Nếu section cung cấp sai, kết quả sẽ tệ hơn B.
  - Áp dụng phù hợp cho báo cáo có cấu trúc chương theo ngành và câu hỏi có phạm vi rõ.
- **Hạn chế:**
  - Mẫu rất nhỏ (1 câu = 6,7 điểm % trên dev, 20 điểm % trên test), một tài liệu duy nhất; không suy rộng.
  - Benchmark do AI soạn nháp, câu filter cố ý mập mờ nên có lợi cho C.
  - Evidence Hit@3 so khớp nguyên văn là tiêu chí khắt khe. A–q19 trượt tự động nhưng ghép top-1 và top-2 vẫn đủ trả lời (grounding 2).
  - Metadata `section` thô: Executive Summary chứa số liệu nhiều ngành nhưng chỉ một nhãn; Box El Niño liên ngành mang nhãn agriculture.
  - Lỗi heading mồ côi (pos09-B) do heading bị nối vào dòng trước khi chuẩn hóa.
  - Thời gian đo trên máy cá nhân, một lần chạy; chi phí ước tính theo đơn giá công bố.
- **Nếu làm lại hoặc mở rộng:**
  1. Trích xuất theo bố cục (PyMuPDF hoặc pdfplumber theo tọa độ) để tách cột và bỏ vùng hình, giảm trộn thứ tự.
  2. So A–B ở cùng kích thước (ví dụ A 800 ký tự) để tách riêng ảnh hưởng của ranh giới đoạn.
  3. Gắn nhãn ngành cho từng đoạn trong Executive Summary, hoặc thêm trường `commodity`.
  4. Thử hybrid BM25 + dense cho các câu nhiều con số.
  5. Mở rộng benchmark, nhờ người khác chấm độc lập và đo độ đồng thuận.

**Sử dụng AI:** Claude hỗ trợ chọn và kiểm tra corpus, soạn nháp manifest, benchmark và code pipeline, chấm nháp nhãn. Tác giả đã chạy toàn bộ pipeline trên máy cá nhân, kiểm chứng evidence bằng script, đọc lại câu hỏi trên PDF và duyệt các nhãn.