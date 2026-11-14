"""Day 06 pipeline: ingest -> chunk (A/B/C) -> embed -> pgvector -> retrieve -> evaluate.

Chạy từ thư mục starter:
    python -m src.run --split dev              # bước 1: chạy dev, chọn cấu hình theo tiêu chí đã chốt
    python -m src.run --split test --no-ingest # bước 2: sau khi chốt, chạy test (dùng lại vector đã lưu)

Kết quả ghi vào starter/results/.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .chunking import chunk_pages
from .embeddings import MyEmbedder          # import trước để nạp starter/.env
from .evaluation import evaluate_query, summarize, write_label_templates
from .ingestion import load_manifest_pages, load_pdf
from .store import PgVectorStore

ROOT = Path(__file__).resolve().parents[1]   # starter/
REPO = ROOT.parent
SOURCE_ID = "wb-cmo-2026-10"

# Tham số 3 cấu hình — giữ nguyên giữa các lần chạy; ghi vào run_config.json.
STRATEGIES = {
    "A": {"chunking": "fixed_size", "params": {"size": 500, "overlap": 50}, "filter": False},
    "B": {"chunking": "by_paragraph", "params": {"max_size": 1000}, "filter": False},
    "C": {"chunking": "by_paragraph", "params": {"max_size": 1000}, "filter": True},
}


def pkg(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not installed"


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def validate_benchmark(questions: list[dict]) -> None:
    dev = [q for q in questions if q["split"] == "dev"]
    test = [q for q in questions if q["split"] == "test"]
    flt = [q for q in questions if q.get("filter")]
    problems = []
    if len(questions) < 20: problems.append(f"chỉ có {len(questions)} câu (cần ≥20)")
    if len(dev) < 15 or len(test) < 5: problems.append(f"dev/test = {len(dev)}/{len(test)} (cần ≥15/≥5)")
    if len(flt) < 3 or not any(q["split"] == "test" for q in flt):
        problems.append("cần ≥3 câu filter, trong đó ≥1 câu test")
    if problems:
        raise SystemExit("Benchmark không hợp lệ: " + "; ".join(problems))


def chunk_stats(chunks) -> dict:
    lengths = [len(c.text) for c in chunks]
    return {"chunks": len(chunks), "chars_total": sum(lengths), "chars_min": min(lengths),
            "chars_median": int(statistics.median(lengths)), "chars_mean": round(statistics.mean(lengths), 1),
            "chars_max": max(lengths)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--strategies", default="A,B,C")
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--no-ingest", action="store_true", help="bỏ qua embed/nạp, dùng vector đã có trong DB")
    ap.add_argument("--manifest", default=str(REPO / "data/manifest.json"))
    ap.add_argument("--pdf", default=str(REPO / "data/raw/CMO-October-2026.pdf"))
    ap.add_argument("--benchmark", default=str(REPO / "benchmark/questions.json"))
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()

    out = Path(args.out)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 1. Corpus ------------------------------------------------------------------
    t0 = time.perf_counter()
    content_pages = load_manifest_pages(Path(args.manifest), SOURCE_ID)
    ingest_log: list[dict] = []
    pages = load_pdf(Path(args.pdf), SOURCE_ID, "data/raw/CMO-October-2026.pdf", content_pages, ingest_log)
    t_read = time.perf_counter() - t0
    if len(pages) < 30:
        raise SystemExit(f"Chỉ đọc được {len(pages)} trang nội dung (cần ≥30)")
    write_json(out / "ingest_log.json", ingest_log)
    print(f"[corpus] {len(pages)} trang nội dung, {len(ingest_log)} dòng log, {t_read:.1f}s")

    # 2. Benchmark ---------------------------------------------------------------
    bench = json.loads(Path(args.benchmark).read_text(encoding="utf-8"))
    questions = bench["questions"]
    validate_benchmark(questions)
    run_qs = [q for q in questions if args.split == "all" or q["split"] == args.split]
    print(f"[benchmark] v{bench.get('benchmark_version')} — chạy {len(run_qs)} câu ({args.split})")

    # 3. Chunk -------------------------------------------------------------------
    chunks = {}
    for s in strategies:
        t = time.perf_counter()
        chunks[s] = chunk_pages(pages, s, **STRATEGIES[s]["params"])
        print(f"[chunk] {s}: {len(chunks[s])} chunks ({time.perf_counter() - t:.2f}s)")
    if "B" in chunks and "C" in chunks:
        assert [c.text for c in chunks["B"]] == [c.text for c in chunks["C"]], "B và C phải cùng chunk"

    # 4. Embed + store -----------------------------------------------------------
    emb = MyEmbedder()
    store = PgVectorStore(os.environ["DATABASE_URL"], emb.dimensions)
    store.create_schema()
    ingest_stats: dict[str, dict] = {}
    if not args.no_ingest:
        vec_cache: dict[str, list[list[float]]] = {}
        for s in strategies:
            key = "B" if s in ("B", "C") else s          # C dùng lại đúng vector của B
            tok_before, t = emb.total_tokens, time.perf_counter()
            if key not in vec_cache:
                vec_cache[key] = emb.embed_documents([c.text for c in chunks[key]])
            t_embed = time.perf_counter() - t
            t = time.perf_counter()
            res = store.upsert(s, chunks[s], vec_cache[key], emb.model_name)
            ingest_stats[s] = {**res, "count_in_db": store.count(s), "embed_seconds": round(t_embed, 2),
                               "embed_tokens": emb.total_tokens - tok_before,
                               "upsert_seconds": round(time.perf_counter() - t, 2),
                               "vectors_reused_from": key if key != s else None}
            print(f"[store] {s}: {res} | db={ingest_stats[s]['count_in_db']}")
    for s in strategies:
        if store.count(s) != len(chunks[s]):
            raise SystemExit(f"DB có {store.count(s)} chunk cho {s} nhưng pipeline tạo {len(chunks[s])}; "
                             "bỏ --no-ingest để nạp lại.")

    # 5. Retrieve + evaluate -----------------------------------------------------
    traces = []
    q_tokens_before = emb.total_tokens
    t = time.perf_counter()
    for q in run_qs:
        qv = emb.embed_query(q["question"])          # một vector cho mọi chiến lược
        for s in strategies:
            use_filter = STRATEGIES[s]["filter"] and bool(q.get("filter"))
            ts = time.perf_counter()
            hits = store.search(s, qv, top_k=args.top_k, filters=q["filter"] if use_filter else None)
            tr = evaluate_query(q, hits, filter_applied=use_filter)
            tr.update({"strategy": s, "search_ms": round((time.perf_counter() - ts) * 1000, 1)})
            traces.append(tr)
    t_eval = time.perf_counter() - t

    metrics = summarize(traces)
    tag = args.split
    (out / f"traces_{tag}.jsonl").write_text(
        "\n".join(json.dumps(tr, ensure_ascii=False) for tr in traces) + "\n", encoding="utf-8")
    write_json(out / f"metrics_{tag}.json", metrics)

    # 6. Thống kê, cấu hình, mẫu nhãn ---------------------------------------------
    write_json(out / "chunk_stats.json", {s: chunk_stats(chunks[s]) for s in strategies})
    write_json(out / f"run_config_{tag}.json", {
        "started_at_utc": started,
        "split": args.split, "top_k": args.top_k, "strategies": {s: STRATEGIES[s] for s in strategies},
        "noise_line_filter": "chunking.is_noise_line (dùng chung cho A/B/C)",
        "embedding": emb.usage(),
        "query_embedding_tokens": emb.total_tokens - q_tokens_before,
        "ingest": ingest_stats or "skipped (--no-ingest)",
        "timing_seconds": {"read_pdf": round(t_read, 2), "retrieve_and_eval": round(t_eval, 2)},
        "store": store.server_info(),
        "benchmark_version": bench.get("benchmark_version"),
        "versions": {"python": platform.python_version(), "pypdf": pkg("pypdf"), "fonttools": pkg("fonttools"),
                     "openai": pkg("openai"), "psycopg": pkg("psycopg")},
    })
    if args.split in ("test", "all"):
        test_traces = [tr for tr in traces if tr["split"] == "test"]
        written = write_label_templates(out / "labels", pages, chunks, test_traces)
        if written:
            print(f"[labels] đã tạo mẫu: {', '.join(written)} (results/labels/)")

    # 7. In bảng kết quả ---------------------------------------------------------
    print(f"\n{'Strategy':<9}{'Split':<6}{'Hit@1':>7}{'Hit@3':>7}  {'Filter ok':<10}{'Missed'}")
    for s in strategies:
        for split, m in metrics.get(s, {}).items():
            print(f"{s:<9}{split:<6}{m['hit_at_1_str']:>7}{m['hit_at_3_str']:>7}  "
                  f"{m['filter_compliant_str']:<10}{', '.join(m['missed_ids']) or '-'}")
    print(f"\nTokens: {emb.total_tokens} | ước tính ${emb.cost_usd():.5f} | kết quả: {out}")


if __name__ == "__main__":
    main()