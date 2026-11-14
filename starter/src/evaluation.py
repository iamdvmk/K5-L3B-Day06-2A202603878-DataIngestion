"""Evaluate retrieved evidence (Hit@1 / Evidence Hit@3), keep per-query traces,
and prepare templates for the manual audits (coherence, metadata, grounding).

Không suy ra nhãn coherence/grounding từ so khớp chuỗi: các nhãn đó do người chấm điền.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any


def canon(text: str) -> str:
    """Chuẩn để so khớp evidence: NFKC + gộp mọi khoảng trắng (kể cả '\\n')."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def gold_pairs(question: dict[str, Any], filter_applied: bool) -> list[tuple[int, str]]:
    """(page, evidence) hợp lệ cho câu hỏi. Bằng chứng outside_filter chỉ hợp lệ khi KHÔNG lọc."""
    pairs = [(question["gold_page"], question["evidence_quote"])]
    for alt in question.get("alternative_evidence", []):
        if filter_applied and alt.get("outside_filter"):
            continue
        pairs.append((alt["gold_page"], alt["evidence_quote"]))
    return pairs


def evaluate_query(question: dict[str, Any], hits: list[dict[str, Any]],
                   filter_applied: bool = False) -> dict[str, Any]:
    """Một hit = chunk thuộc gold_source_id, nằm ở trang gold và chứa ĐỦ evidence của trang đó.

    Ghi thêm cờ phân tích lỗi:
      - page_hit_no_evidence: lấy đúng trang gold nhưng chunk không chứa trọn evidence
        (thường do evidence bị cắt qua 2 chunk).
      - filter_compliant: mọi kết quả đều đúng section yêu cầu (chỉ khi câu hỏi có filter).
    """
    pairs = gold_pairs(question, filter_applied)
    gold_pages = {p for p, _ in pairs}
    want_section = (question.get("filter") or {}).get("section")

    scored = []
    for h in hits:
        same_doc = h.get("document_id") == question["gold_source_id"]
        text = canon(h.get("text", ""))
        matched = [p for p, ev in pairs if same_doc and h.get("page") == p and canon(ev) in text]
        scored.append({
            "rank": h["rank"],
            "chunk_id": h["chunk_id"],
            "document_id": h.get("document_id"),
            "source": h.get("source"),
            "page": h.get("page"),
            "section": h.get("section"),
            "score": round(h.get("score", 0.0), 6),
            "evidence_hit": bool(matched),
            "page_hit_no_evidence": same_doc and h.get("page") in gold_pages and not matched,
            "section_matches_filter": (h.get("section") == want_section) if want_section else None,
            "text": h.get("text", ""),
            "metadata": h.get("metadata", {}),
        })

    first = next((s["rank"] for s in scored if s["evidence_hit"]), None)
    return {
        "question_id": question["id"],
        "split": question["split"],
        "type": question.get("type"),
        "question": question["question"],
        "expected_answer": question.get("expected_answer"),
        "filter": question.get("filter"),
        "filter_applied": filter_applied,
        "gold": [{"page": p, "evidence_quote": ev} for p, ev in pairs],
        "hit_at_1": bool(scored) and scored[0]["evidence_hit"],
        "hit_at_3": any(s["evidence_hit"] for s in scored[:3]),
        "first_hit_rank": first,
        "filter_compliant": (all(s["section_matches_filter"] for s in scored) if want_section else None),
        "hits": scored,
    }


def summarize(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Hit@1 và Evidence Hit@3 theo strategy × split, dạng số đếm n/N (không chỉ %)."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in traces:
        groups[(t["strategy"], t["split"])].append(t)

    out: dict[str, Any] = {}
    for (strategy, split), ts in sorted(groups.items()):
        n = len(ts)
        h1 = sum(t["hit_at_1"] for t in ts)
        h3 = sum(t["hit_at_3"] for t in ts)
        fq = [t for t in ts if t["filter"]]
        fc = sum(bool(t["filter_compliant"]) for t in fq)
        out.setdefault(strategy, {})[split] = {
            "N": n,
            "hit_at_1": h1, "hit_at_1_str": f"{h1}/{n}", "hit_at_1_rate": round(h1 / n, 4) if n else None,
            "hit_at_3": h3, "hit_at_3_str": f"{h3}/{n}", "hit_at_3_rate": round(h3 / n, 4) if n else None,
            "filter_questions": len(fq),
            "filter_compliant_str": f"{fc}/{len(fq)}" if fq else "N/A",
            "page_hit_no_evidence": sum(any(h["page_hit_no_evidence"] for h in t["hits"]) for t in ts),
            "missed_ids": [t["question_id"] for t in ts if not t["hit_at_3"]],
        }
    return out


# ---------------------------------------------------------------------------
# Mẫu nhãn thủ công (CSV, mở bằng Excel). Không ghi đè file đã có để tránh mất nhãn.
# ---------------------------------------------------------------------------
def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig để Excel đọc đúng
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return True


def coherence_anchors(pages: list[Any], n: int = 10) -> list[dict[str, Any]]:
    """Chọn n vị trí cố định TRƯỚC khi xem điểm: n trang được tính đầu tiên (theo số trang);
    mỗi trang lấy câu đầu tiên dài ≥ 80 ký tự bắt đầu sau 40% độ dài văn bản đã làm sạch."""
    from .chunking import clean_lines

    anchors = []
    for page in sorted(pages, key=lambda p: p.page)[:n]:
        text = " ".join(clean_lines(page.text))
        start = int(len(text) * 0.4)
        sentences = re.split(r"(?<=[.!?])\s+", text[start:])
        sent = next((s for s in sentences[1:] if len(s) >= 80), sentences[0])
        anchors.append({"anchor_id": f"pos{len(anchors) + 1:02d}", "page": page.page, "anchor_text": sent[:300]})
    return anchors


def write_label_templates(out_dir: Path, pages: list[Any], chunks_by_strategy: dict[str, list[Any]],
                          test_traces: list[dict[str, Any]]) -> list[str]:
    written = []

    # 1) Coherence: cùng 10 vị trí cho mỗi chiến lược (C dùng chunk giống B).
    rows = []
    for a in coherence_anchors(pages):
        anchor = canon(a["anchor_text"])
        for strategy, chunks in chunks_by_strategy.items():
            on_page = [c for c in chunks if c.page == a["page"]]
            whole = [c for c in on_page if anchor in canon(c.text)]
            # Vị trí bị cắt qua 2 chunk (thường gặp ở A): chấm chunk chứa phần đầu của câu.
            start = [c for c in on_page if anchor[:40] in canon(c.text)]
            c = (whole or start or [None])[0]
            rows.append([a["anchor_id"], a["page"], a["anchor_text"], strategy,
                         c.chunk_id if c else "NOT_FOUND", not whole, c.text if c else "", "", ""])
    if _write_csv(out_dir / "coherence_labels.csv",
                  ["anchor_id", "page", "anchor_text", "strategy", "chunk_id", "anchor_split_across_chunks",
                   "chunk_text", "label_0_1_2", "reason"], rows):
        written.append("coherence_labels.csv")

    # 2) Metadata: 5 câu test × top-3 × chiến lược.
    rows = []
    for t in test_traces:
        for h in t["hits"]:
            rows.append([t["strategy"], t["question_id"], h["rank"], h["chunk_id"], h["source"],
                         h["page"], h["metadata"].get("printed_page"), h["section"],
                         (t["filter"] or {}).get("section", ""), h["section_matches_filter"],
                         "", ""])
    if _write_csv(out_dir / "metadata_audit.csv",
                  ["strategy", "question_id", "rank", "chunk_id", "source", "pdf_page", "printed_page",
                   "section", "required_filter", "auto_section_matches_filter",
                   "metadata_correct_Y_N", "error_note"], rows):
        written.append("metadata_audit.csv")

    # 3) Grounding: mỗi (chiến lược, câu test) một dòng, chấm top-3 gộp.
    rows = []
    for t in test_traces:
        tops = [f"[p{h['page']}] {h['text']}" for h in t["hits"][:3]] + ["", "", ""]
        rows.append([t["strategy"], t["question_id"], t["question"], t["expected_answer"],
                     t["hit_at_3"], *tops[:3], "", ""])
    if _write_csv(out_dir / "grounding_labels.csv",
                  ["strategy", "question_id", "question", "expected_answer", "auto_evidence_hit_at_3",
                   "top1", "top2", "top3", "label_0_1_2", "reason"], rows):
        written.append("grounding_labels.csv")
    return written