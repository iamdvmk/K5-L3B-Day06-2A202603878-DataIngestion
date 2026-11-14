"""Read and normalize source data. TODO: implement functions."""
from __future__ import annotations
from pathlib import Path
from .models import DocumentPage

import json
import re
import unicodedata
import pypdf

from .models import DocumentPage

# Tiêu đề lặp ở đầu mỗi trang của báo cáo CMO, ví dụ:
#   "ENERGYCOMMODITY MARKETS OUTLOOK | OCTOBER 2026\n14\n..."
# pypdf có thể dính tên section vào "COMMODITY" và thêm khoảng trắng thừa.
_HEADER_RE = re.compile(
    r"^\s*(?P<section>[A-Z][A-Z ]*?)\s*COMMODITY MARKETS OUTLOOK\s*\|\s*OCTOBER 2026[ \t]*\n"
    r"\s*(?P<printed>\d+)[ \t]*\n"
)

def load_manifest_pages(manifest_path: Path, source_id: str) -> dict[int, dict[str, Any]]:
    """Trả về {pdf_page: entry} cho các trang included=true của source_id trong manifest."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    for src in manifest["sources"]:
        if src["source_id"] == source_id:
            return {p["pdf_page"]: p for p in src["pages"] if p["included"]}
    raise ValueError(f"source_id {source_id!r} không có trong manifest")


def split_running_header(raw: str) -> tuple[str | None, int | None, str]:
    """Tách tiêu đề đầu trang thành (section, printed_page, phần thân).

    section trả về dạng slug khớp manifest, ví dụ "metals_and_minerals".
    Không khớp mẫu thì trả về (None, None, raw) để nơi gọi ghi log.
    """
    m = _HEADER_RE.match(raw)
    if not m:
        return None, None, raw
    section = re.sub(r"\s+", "_", m.group("section").strip().lower())
    return section, int(m.group("printed")), raw[m.end():]

def load_pdf(
    path: Path,
    document_id: str,
    source: str,
    content_pages: dict[int, dict[str, Any]] | set[int] | None = None,
    log: list[dict[str, Any]] | None = None,
) -> list[DocumentPage]:
    """Đọc từng trang vật lý, giữ số trang PDF (bắt đầu từ 1) và nguồn.

    - content_pages: các trang được tính (từ load_manifest_pages). None = lấy mọi trang có text.
    - log: list để ghi trang rỗng/bị loại/lỗi trích xuất/không có header (không âm thầm bỏ qua).
    Không OCR: corpus này có text layer; trang rỗng chỉ được ghi log.
    """
    log = log if log is not None else []
    reader = pypdf.PdfReader(str(path))
    manifest_entries = content_pages if isinstance(content_pages, dict) else {}
    allowed = set(content_pages) if content_pages is not None else None
    pages: list[DocumentPage] = []

    for page_no, pdf_page in enumerate(reader.pages, start=1):
        try:
            raw = pdf_page.extract_text() or ""
        except Exception as exc:  # trang hỏng không được làm dừng cả pipeline
            log.append({"page": page_no, "status": "extract_error", "detail": repr(exc)})
            continue

        if not raw.strip():
            log.append({"page": page_no, "status": "empty", "detail": "no text layer / blank"})
            continue
        if allowed is not None and page_no not in allowed:
            reason = "not in counted pages"
            log.append({"page": page_no, "status": "excluded", "detail": reason})
            continue

        section, printed, body = split_running_header(raw)
        if section is None:
            log.append({"page": page_no, "status": "warning", "detail": "running header not found"})

        expected = manifest_entries.get(page_no, {}).get("section")
        if expected and section and expected != section:
            log.append({"page": page_no, "status": "warning",
                        "detail": f"header section {section!r} != manifest {expected!r}"})
        section = section or expected

        text = normalize_text(body)
        if len(text) < 200:
            log.append({"page": page_no, "status": "warning", "detail": f"short page ({len(text)} chars)"})

        pages.append(DocumentPage(
            document_id=document_id,
            source=source,
            page=page_no,
            text=text,
            metadata={
                "section": section,
                "printed_page": printed,
                "raw_chars": len(raw),
                "normalized_chars": len(text),
                "extractor": f"pypdf {pypdf.__version__}",
            },
        ))
    return pages
    raise NotImplementedError("TODO: load_pdf")


def normalize_text(text: str) -> str:
    """TODO: normalize Unicode and whitespace without discarding useful structure."""
    text = unicodedata.normalize("NFKC", text)          # ligature ﬁ -> fi, full-width...
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("­", "")                    # soft hyphen
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")

    parts: list[str] = []
    for line in text.split("\n"):
        soft_wrap = line.endswith((" ", "\t"))
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            if parts and parts[-1] != "\n":
                parts.append("\n")
            continue
        parts.append(line)
        parts.append(" " if soft_wrap else "\n")

    out = "".join(parts)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r" *\n *", "\n", out)
    out = re.sub(r"\n{2,}", "\n", out)
    return out.strip()
    raise NotImplementedError("TODO: normalize_text")
