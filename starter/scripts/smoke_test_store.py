"""Smoke test: embed vài chunk thật, nạp 2 lần (kiểm tra không nhân đôi), tìm kiếm có/không filter.

Chạy từ thư mục starter:  python scripts/smoke_test_store.py
Dùng bảng riêng 'chunks_smoke' để không đụng dữ liệu thí nghiệm. Tốn vài trăm token.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.chunking import chunk_pages            # noqa: E402
from src.embeddings import MyEmbedder           # noqa: E402
from src.ingestion import load_manifest_pages, load_pdf  # noqa: E402
from src.store import PgVectorStore             # noqa: E402

REPO = ROOT.parent
cp = load_manifest_pages(REPO / "data/manifest.json", "wb-cmo-2026-10")
pages = load_pdf(REPO / "data/raw/CMO-October-2026.pdf", "wb-cmo-2026-10",
                 "data/raw/CMO-October-2026.pdf", cp)
chunks = [c for c in chunk_pages(pages, "B", max_size=1000) if c.page in (22, 47, 49)]

emb = MyEmbedder()
vectors = emb.embed_documents([c.text for c in chunks])
store = PgVectorStore(os.environ["DATABASE_URL"], emb.dimensions, table="chunks_smoke")
store.create_schema()

print("load 1:", store.upsert("B", chunks, vectors, emb.model_name), "count =", store.count("B"))
print("load 2:", store.upsert("B", chunks, vectors, emb.model_name), "count =", store.count("B"))

qv = emb.embed_query("How much is the sector's price index projected to rise in 2026?")
for f in (None, {"section": "fertilizers"}):
    print("\nfilter =", f)
    for h in store.search("B", qv, top_k=3, filters=f):
        print(f"  #{h['rank']} p{h['page']} {h['section']:<20} score={h['score']:.3f}  {h['text'][:70]!r}")
print("\nusage:", emb.usage())