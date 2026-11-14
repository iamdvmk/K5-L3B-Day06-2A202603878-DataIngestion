"""Real embedding provider: OpenAI embeddings API. Không bao giờ fallback sang vector giả."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

# Nạp starter/.env theo vị trí file, không phụ thuộc thư mục đang đứng.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Số chiều mặc định của các model OpenAI.
KNOWN_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}
# USD / 1 triệu token. KIỂM TRA LẠI trên trang giá OpenAI trước khi ghi vào báo cáo.
PRICE_PER_1M_TOKENS = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
}


class Embedder(Protocol):
    model_name: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order."""
        ...

    def embed_query(self, question: str) -> list[float]:
        """Return a query vector compatible with the stored document vectors."""
        ...


class MyEmbedder:
    """Adapter cho OpenAI Embeddings API.

    - Query và document dùng cùng model, cùng cách xử lý (model này không cần prefix).
    - Gửi theo batch; SDK tự retry khi gặp rate limit / lỗi mạng (max_retries).
    - Ghi lại token đã dùng để tính chi phí thật (self.total_tokens, self.cost_usd()).
    - Lỗi API -> raise, KHÔNG thay bằng vector ngẫu nhiên.
    """

    def __init__(self, model_name: str | None = None, batch_size: int = 100,
                 max_retries: int = 5) -> None:
        from openai import OpenAI  # import ở đây để báo lỗi rõ nếu chưa cài SDK

        self.model_name = model_name or os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small"
        if self.model_name not in KNOWN_DIMENSIONS:
            raise ValueError(f"Model {self.model_name!r} chưa có trong KNOWN_DIMENSIONS; thêm số chiều thủ công.")
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("Thiếu OPENAI_API_KEY trong starter/.env")

        self.dimensions = KNOWN_DIMENSIONS[self.model_name]
        self.batch_size = batch_size
        self.client = OpenAI(max_retries=max_retries)
        self.total_tokens = 0
        self.n_requests = 0
        self.seconds = 0.0

    # -- nội bộ ---------------------------------------------------------------
    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        t0 = time.perf_counter()
        resp = self.client.embeddings.create(model=self.model_name, input=batch)
        self.seconds += time.perf_counter() - t0
        self.n_requests += 1
        self.total_tokens += resp.usage.total_tokens

        data = sorted(resp.data, key=lambda d: d.index)   # giữ đúng thứ tự đầu vào
        vectors = [d.embedding for d in data]
        if len(vectors) != len(batch):
            raise RuntimeError(f"API trả {len(vectors)} vector cho {len(batch)} đầu vào")
        for v in vectors:
            if len(v) != self.dimensions:
                raise RuntimeError(f"Vector {len(v)} chiều, kỳ vọng {self.dimensions}")
        return vectors

    # -- API công khai ------------------------------------------------------------
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if any(not t or not t.strip() for t in texts):
            raise ValueError("Có text rỗng trong danh sách cần embed")
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._embed_batch(texts[i:i + self.batch_size]))
        return out

    def embed_query(self, question: str) -> list[float]:
        if not question.strip():
            raise ValueError("Câu hỏi rỗng")
        return self._embed_batch([question])[0]

    def cost_usd(self) -> float:
        return self.total_tokens / 1_000_000 * PRICE_PER_1M_TOKENS[self.model_name]

    def usage(self) -> dict:
        return {"provider": "openai", "model": self.model_name, "dimensions": self.dimensions,
                "requests": self.n_requests, "total_tokens": self.total_tokens,
                "api_seconds": round(self.seconds, 2), "est_cost_usd": round(self.cost_usd(), 6)}