"""Store and search vectors using PostgreSQL + pgvector (cosine distance, exact search)."""
from __future__ import annotations

import json
from typing import Any

import psycopg

from .models import Chunk

# Chỉ cho phép lọc trên các cột này (tránh SQL injection qua tên cột).
ALLOWED_FILTERS = {"section": "section", "document_id": "document_id", "page": "page"}


def _to_vector_literal(vec: list[float]) -> str:
    """pgvector nhận chuỗi dạng '[0.1,0.2,...]' rồi ép kiểu ::vector."""
    return "[" + ",".join(f"{x:.8g}" for x in vec) + "]"


class PgVectorStore:
    """Một bảng cho mọi chiến lược; khóa chính (strategy, chunk_id).

    Tìm kiếm: exact (quét tuần tự, không index ANN) theo cosine distance `<=>`.
    Với ~1.000 chunk, exact search đủ nhanh và cho kết quả chính xác tuyệt đối.
    """

    def __init__(self, database_url: str, dimensions: int, table: str = "chunks") -> None:
        if not database_url:
            raise ValueError("Thiếu DATABASE_URL")
        if not isinstance(dimensions, int) or not 0 < dimensions <= 16000:
            raise ValueError(f"dimensions không hợp lệ: {dimensions}")
        if not table.isidentifier():
            raise ValueError("Tên bảng không hợp lệ")
        self.database_url = database_url
        self.dimensions = dimensions
        self.table = table

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, connect_timeout=5)

    # ------------------------------------------------------------------ schema
    def create_schema(self) -> None:
        t, d = self.table, self.dimensions
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {t} (
                    strategy        text        NOT NULL,
                    chunk_id        text        NOT NULL,
                    document_id     text        NOT NULL,
                    source          text        NOT NULL,
                    page            integer     NOT NULL,
                    section         text,
                    content         text        NOT NULL,
                    content_hash    text        NOT NULL,
                    metadata        jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
                    embedding_model text        NOT NULL,
                    embedding       vector({d}) NOT NULL,
                    ingested_at     timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (strategy, chunk_id)
                )""")
            cur.execute(f"CREATE INDEX IF NOT EXISTS {t}_strategy_section_idx ON {t} (strategy, section)")
            # Bảng đã tồn tại với số chiều khác (đổi model) -> báo lỗi thay vì ghi sai.
            cur.execute("""SELECT atttypmod FROM pg_attribute
                           WHERE attrelid = %s::regclass AND attname = 'embedding'""", (t,))
            existing = cur.fetchone()[0]
            if existing != d:
                raise RuntimeError(f"Bảng {t} có vector({existing}), model hiện tại cần vector({d}).")

    # ------------------------------------------------------------------ write
    def upsert(self, strategy: str, chunks: list[Chunk], vectors: list[list[float]],
               embedding_model: str) -> dict[str, int]:
        """Ghi chunk theo ID ổn định, không nhân đôi khi nạp lại.

        - Chunk mới -> INSERT; chunk đổi nội dung/model -> UPDATE; giống hệt -> bỏ qua.
        - Chunk cũ của cùng (strategy, document_id) không còn trong lần nạp này -> DELETE
          (xử lý khi tài liệu hoặc tham số chunk thay đổi).
        Toàn bộ chạy trong một transaction.
        """
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunk nhưng {len(vectors)} vector")
        if not chunks:
            return {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
        for v in vectors:
            if len(v) != self.dimensions:
                raise ValueError(f"Vector {len(v)} chiều, store cần {self.dimensions}")
        ids = [c.chunk_id for c in chunks]
        if len(set(ids)) != len(ids):
            raise ValueError("chunk_id bị trùng trong cùng một lần nạp")

        t = self.table
        sql = f"""
            INSERT INTO {t} (strategy, chunk_id, document_id, source, page, section, content,
                             content_hash, metadata, embedding_model, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::vector)
            ON CONFLICT (strategy, chunk_id) DO UPDATE SET
                document_id = EXCLUDED.document_id, source = EXCLUDED.source,
                page = EXCLUDED.page, section = EXCLUDED.section, content = EXCLUDED.content,
                content_hash = EXCLUDED.content_hash, metadata = EXCLUDED.metadata,
                embedding_model = EXCLUDED.embedding_model, embedding = EXCLUDED.embedding,
                ingested_at = now()
            WHERE ({t}.content_hash, {t}.embedding_model)
                  IS DISTINCT FROM (EXCLUDED.content_hash, EXCLUDED.embedding_model)
            RETURNING (xmax = 0) AS inserted"""
        stats = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
        with self._connect() as conn, conn.cursor() as cur:
            for c, v in zip(chunks, vectors):
                cur.execute(sql, (
                    strategy, c.chunk_id, c.document_id, c.source, c.page,
                    c.metadata.get("section"), c.text, c.metadata.get("content_hash", ""),
                    json.dumps(c.metadata, ensure_ascii=False), embedding_model,
                    _to_vector_literal(v),
                ))
                row = cur.fetchone()
                if row is None:
                    stats["unchanged"] += 1
                elif row[0]:
                    stats["inserted"] += 1
                else:
                    stats["updated"] += 1
            doc_ids = sorted({c.document_id for c in chunks})
            cur.execute(f"""DELETE FROM {t}
                            WHERE strategy = %s AND document_id = ANY(%s) AND NOT (chunk_id = ANY(%s))""",
                        (strategy, doc_ids, ids))
            stats["deleted"] = cur.rowcount
        return stats

    # ------------------------------------------------------------------ read
    def search(self, strategy: str, query_vector: list[float], top_k: int = 3,
               filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Lọc metadata trong SQL (WHERE, tham số hóa), xếp theo cosine distance, trả top-k.

        score = 1 - cosine_distance (càng cao càng giống).
        """
        if len(query_vector) != self.dimensions:
            raise ValueError(f"Query vector {len(query_vector)} chiều, store cần {self.dimensions}")
        where, params = ["strategy = %s"], [strategy]
        for key, value in (filters or {}).items():
            if key not in ALLOWED_FILTERS:
                raise ValueError(f"Không cho phép lọc theo {key!r}; chỉ {sorted(ALLOWED_FILTERS)}")
            where.append(f"{ALLOWED_FILTERS[key]} = %s")
            params.append(value)
        q = _to_vector_literal(query_vector)
        sql = f"""
            SELECT chunk_id, document_id, source, page, section, content, metadata,
                   1 - (embedding <=> %s::vector) AS score
            FROM {self.table}
            WHERE {' AND '.join(where)}
            ORDER BY embedding <=> %s::vector
            LIMIT %s"""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, [q, *params, q, top_k])
            rows = cur.fetchall()
        return [
            {"rank": i + 1, "chunk_id": r[0], "document_id": r[1], "source": r[2], "page": r[3],
             "section": r[4], "text": r[5], "metadata": r[6], "score": float(r[7])}
            for i, r in enumerate(rows)
        ]

    def count(self, strategy: str | None = None) -> int:
        """Đếm chunk (để kiểm tra nạp lại không nhân đôi)."""
        with self._connect() as conn, conn.cursor() as cur:
            if strategy:
                cur.execute(f"SELECT count(*) FROM {self.table} WHERE strategy = %s", (strategy,))
            else:
                cur.execute(f"SELECT count(*) FROM {self.table}")
            return cur.fetchone()[0]

    def server_info(self) -> dict[str, str]:
        """Phiên bản PostgreSQL và pgvector (ghi vào run_config để tái lập)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_setting('server_version'), "
                        "(SELECT extversion FROM pg_extension WHERE extname = 'vector')")
            pg, vec = cur.fetchone()
        return {"postgresql": pg, "pgvector": vec, "table": self.table, "search": "exact, cosine (<=>)"}