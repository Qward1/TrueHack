"""Retrieval-Augmented Generation index over Lua documentation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import structlog

from src.tools.base import Tool

logger = structlog.get_logger(__name__)

_CHUNK_SIZE = 500
_CHUNK_OVERLAP = 100
_METADATA_FILE = "metadata.json"
_INDEX_FILE = "index.faiss"


def _chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split *text* into overlapping character-level chunks."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start += size - overlap
    return chunks


class LuaRAG(Tool):
    """FAISS-backed retriever for Lua documentation chunks."""

    def __init__(self, docs_path: str, index_path: str) -> None:
        self._docs_path = Path(docs_path)
        self._index_path = Path(index_path)
        self._model = None   # lazy-loaded SentenceTransformer
        self._index = None   # lazy-loaded faiss.Index
        self._metadata: list[dict] = []

    async def run(self, query: str, **kwargs: object) -> dict:
        """Primary tool entry point."""
        results = await self.search(query, top_k=int(kwargs.get("top_k", 3)))
        return {"results": results}

    def _get_model(self):
        """Lazy-load the embedding model (downloaded once, cached by HF)."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            logger.info("rag_loading_model", model="all-MiniLM-L6-v2")
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
        return self._model

    def build_index(self) -> None:
        """Read docs, chunk, embed, build FAISS index, persist to disk."""
        import faiss

        docs: list[tuple[str, str]] = []  # (text, source)
        for ext in ("*.md", "*.txt"):
            for fpath in self._docs_path.rglob(ext):
                text = fpath.read_text(encoding="utf-8", errors="replace")
                for chunk in _chunk_text(text):
                    if chunk.strip():
                        docs.append((chunk, fpath.name))

        if not docs:
            logger.warning("rag_no_docs", docs_path=str(self._docs_path))
            docs = [("No Lua documentation available.", "empty")]

        texts = [d[0] for d in docs]
        sources = [d[1] for d in docs]

        model = self._get_model()
        logger.info("rag_embedding", n_chunks=len(texts))
        embeddings = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)
        embeddings = embeddings.astype(np.float32)
        faiss.normalize_L2(embeddings)

        dim = embeddings.shape[1]
        index = faiss.IndexFlatIP(dim)  # inner product == cosine after normalisation
        index.add(embeddings)

        self._index_path.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(self._index_path / _INDEX_FILE))

        metadata = [{"text": t, "source": s} for t, s in zip(texts, sources)]
        (self._index_path / _METADATA_FILE).write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("rag_index_built", n_chunks=len(metadata))

        self._index = index
        self._metadata = metadata

    def _load_index(self) -> None:
        """Load an existing FAISS index from disk."""
        import faiss

        self._index = faiss.read_index(str(self._index_path / _INDEX_FILE))
        self._metadata = json.loads(
            (self._index_path / _METADATA_FILE).read_text(encoding="utf-8")
        )
        logger.debug("rag_index_loaded", n_chunks=len(self._metadata))

    async def search(self, query: str, top_k: int = 3) -> list[dict]:
        """Return *top_k* most relevant chunks for *query*.

        Auto-builds the index on first call if it doesn't exist on disk.

        Each result: ``{"text": str, "source": str, "score": float}``
        """
        index_file = self._index_path / _INDEX_FILE
        if self._index is None:
            if index_file.exists():
                self._load_index()
            else:
                self.build_index()

        model = self._get_model()
        import faiss

        q_emb = model.encode([query], show_progress_bar=False, convert_to_numpy=True).astype(np.float32)
        faiss.normalize_L2(q_emb)

        k = min(top_k, len(self._metadata))
        scores, indices = self._index.search(q_emb, k)

        results: list[dict] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            meta = self._metadata[idx]
            results.append(
                {"text": meta["text"], "source": meta["source"], "score": float(score)}
            )
        return results
