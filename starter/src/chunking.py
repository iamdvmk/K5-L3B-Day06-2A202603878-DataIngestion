"""Implement fixed-size and structure-aware chunking."""
from __future__ import annotations

import hashlib
import re
from .models import Chunk, DocumentPage

# ---------------------------------------------------------------------------
# Tiền xử lý dùng CHUNG cho mọi chiến lược, để so sánh A–B chỉ khác cách chia chunk.
# ---------------------------------------------------------------------------
_NUMERIC_TOKEN = re.compile(r"^[-+−]?[\d.,%$/()–-]+$")

def is_noise_line(line: str) -> bool:
    """Nhận diện dòng rác do trích xuất biểu đồ (nhãn trục, số liệu rời).

    Heuristic (ghi vào báo cáo): bỏ dòng nếu
      - ít hơn 4 từ VÀ có chữ số (ví dụ "50", "Jan-26", "2025 2026 2027mb/d"), hoặc
      - quá nửa số token là số (ví dụ "-8 -6 -4 -2 0 2 Australia Indonesia").
    Giữ lại heading ngắn không có số ("Oil", "Recent developments", "Outlook").
    Hạn chế: chú giải biểu đồ dạng chữ ("Brent oil", "Gold Silver") vẫn lọt qua.
    """
    tokens = line.split()
    if not tokens:
        return True
    numeric = sum(bool(_NUMERIC_TOKEN.match(t)) for t in tokens)
    if len(tokens) < 4 and any(ch.isdigit() for ch in line):
        return True
    return numeric / len(tokens) > 0.5

def clean_lines(text: str) -> list[str]:
    """Tách text đã chuẩn hóa theo '\\n' và bỏ các dòng rác."""
    return [ln.strip() for ln in text.split("\n") if ln.strip() and not is_noise_line(ln)]

def _make_chunk(page: DocumentPage, strategy: str, index: int, text: str, extra: dict) -> Chunk:
    # ID ổn định: cùng tài liệu + chiến lược + trang + thứ tự -> cùng ID khi nạp lại.
    chunk_id = f"{page.document_id}:{strategy}:p{page.page:03d}:c{index:03d}"
    metadata = {
        **page.metadata,              # section, printed_page, ... từ ingestion
        "strategy": strategy,
        "chunk_index": index,
        "chars": len(text),
        # hash nội dung: phát hiện chunk đổi khi tài liệu/tham số đổi (để upsert/xóa chunk cũ)
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        **extra,
    }
    return Chunk(chunk_id=chunk_id, document_id=page.document_id, source=page.source,
                 page=page.page, text=text, metadata=metadata)


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z“\"(])")


def _split_long(text: str, max_size: int) -> list[str]:
    """Cắt đoạn quá dài: ưu tiên ranh giới câu, câu nào vẫn quá dài thì cắt cứng."""
    pieces: list[str] = []
    current = ""
    for sent in _SENTENCE_SPLIT.split(text):
        while len(sent) > max_size:                    # câu dài bất thường
            if current:
                pieces.append(current)
                current = ""
            cut = sent.rfind(" ", 0, max_size)
            cut = cut if cut > max_size // 2 else max_size
            pieces.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if current and len(current) + 1 + len(sent) > max_size:
            pieces.append(current)
            current = sent
        else:
            current = f"{current} {sent}".strip()
    if current:
        pieces.append(current)
    return pieces


def _is_heading(line: str) -> bool:
    """Heading ngắn: ≤ 6 từ, không kết thúc bằng dấu câu (ví dụ 'Recent developments')."""
    return len(line.split()) <= 6 and not line.rstrip().endswith((".", ":", ";", ","))

def fixed_size(page: DocumentPage, size: int = 500, overlap: int = 50,
               strategy: str = "A") -> list[Chunk]:
    """TODO: split text into overlapping windows and retain page metadata."""
    if not 0 <= overlap < size:
        raise ValueError("cần 0 <= overlap < size")
    text = " ".join(clean_lines(page.text))
    step = size - overlap
    chunks: list[Chunk] = []
    for index, start in enumerate(range(0, max(len(text) - overlap, 1), step)):
        piece = text[start:start + size].strip()
        if piece:
            chunks.append(_make_chunk(page, strategy, index, piece, {
                "chunking": "fixed_size", "size": size, "overlap": overlap,
                "char_start": start, "char_end": min(start + size, len(text)),
            }))
    return chunks
    raise NotImplementedError("TODO: fixed_size")


def by_paragraph(page: DocumentPage, max_size: int = 1000, strategy: str = "B") -> list[Chunk]:
    """TODO: group paragraphs, split oversized paragraphs and retain metadata."""
    groups: list[list[str]] = []
    current: list[str] = []

    def length(parts: list[str]) -> int:
        return sum(len(p) for p in parts) + max(len(parts) - 1, 0)

    def flush() -> None:
        nonlocal current
        carry: list[str] = []
        while current and _is_heading(current[-1]):     # không để heading "mồ côi" cuối chunk
            carry.insert(0, current.pop())
        if current:
            groups.append(current)
        current = carry

    for para in clean_lines(page.text):
        for piece in (_split_long(para, max_size) if len(para) > max_size else [para]):
            if current and length(current) + 1 + len(piece) > max_size:
                flush()
            current.append(piece)
    if current:
        groups.append(current)

    return [
        _make_chunk(page, strategy, i, "\n".join(parts), {
            "chunking": "by_paragraph", "max_size": max_size, "n_paragraphs": len(parts),
        })
        for i, parts in enumerate(groups)
    ]
    raise NotImplementedError("TODO: by_paragraph")

def chunk_pages(pages: list[DocumentPage], strategy: str, **params) -> list[Chunk]:
    """Áp dụng một chiến lược cho cả corpus. strategy: 'A' (fixed) | 'B' | 'C' (paragraph)."""
    out: list[Chunk] = []
    for page in pages:
        if strategy == "A":
            out.extend(fixed_size(page, strategy="A", **params))
        elif strategy in ("B", "C"):
            out.extend(by_paragraph(page, strategy=strategy, **params))
        else:
            raise ValueError(f"unknown strategy {strategy!r}")
    return out
