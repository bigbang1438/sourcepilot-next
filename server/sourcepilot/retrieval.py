"""BM25 lexical retrieval and real cosine vectors fused with reciprocal ranks."""

import json
import re
import time

import numpy as np
from rank_bm25 import BM25Okapi


def tokenize(text):
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower())
    cjk = re.findall(r"[\u4e00-\u9fff]+", text)
    return words + [part[i : i + 2] for part in cjk for i in range(len(part) - 1)]


class Retriever:
    def __init__(self, store, models):
        self.store = store
        self.models = models

    def search(self, query, limit=6, snapshot=None, mode="hybrid", timeout=90):
        started = time.monotonic()
        snapshot = self.store.snapshot() if snapshot is None else snapshot
        chunks = self.store.chunks(snapshot)
        if not chunks:
            return {"hits": [], "mode": "bm25" if mode != "vector" else "vector", "reranked": False}
        tokens = [tokenize(chunk["text"]) for chunk in chunks]
        query_tokens = tokenize(query)
        lexical_scores = BM25Okapi(tokens).get_scores(query_tokens)
        overlap = set(query_tokens)
        lexical = sorted(
            (i for i in range(len(chunks)) if overlap.intersection(tokens[i])),
            key=lambda i: float(lexical_scores[i]),
            reverse=True,
        )
        vector = []
        actual_mode = mode
        if mode != "bm25":
            try:
                query_vector = np.asarray(
                    self.models.embed([query], timeout=timeout)[0], dtype=float
                )
                if (
                    query_vector.ndim != 1
                    or not np.isfinite(query_vector).all()
                    or np.linalg.norm(query_vector) == 0
                ):
                    raise ValueError("invalid query vector")
                similarities = {}
                for i, chunk in enumerate(chunks):
                    if not chunk["vector"]:
                        continue
                    candidate = np.asarray(json.loads(chunk["vector"]), dtype=float)
                    if (
                        candidate.shape == query_vector.shape
                        and np.isfinite(candidate).all()
                        and np.linalg.norm(candidate) > 0
                    ):
                        similarities[i] = float(
                            np.dot(query_vector, candidate)
                            / (np.linalg.norm(query_vector) * np.linalg.norm(candidate))
                        )
                vector = sorted(similarities, key=lambda index: similarities[index], reverse=True)
                if not vector:
                    actual_mode = "bm25"
            except (RuntimeError, ValueError, IndexError):
                actual_mode = "bm25"
        ranks = {}
        lists = (
            [lexical]
            if actual_mode == "bm25"
            else [vector]
            if actual_mode == "vector"
            else [lexical, vector]
        )
        for ranking in lists:
            for position, index in enumerate(ranking, 1):
                ranks[index] = ranks.get(index, 0.0) + 1.0 / (60 + position)
        hits = []
        seen = set()
        for index in sorted(ranks, key=lambda index: ranks[index], reverse=True):
            chunk = chunks[index]
            page_key = (chunk["document_id"], chunk["version"], chunk["page"])
            if page_key in seen:
                continue
            seen.add(page_key)
            hits.append(
                {
                    "documentId": chunk["document_id"],
                    "name": chunk["name"],
                    "version": chunk["version"],
                    "page": chunk["page"],
                    "text": chunk["text"],
                    "score": ranks[index],
                }
            )
            if len(hits) >= limit:
                break
        reranked = False
        warning = None
        if hits and hasattr(self.models, "rerank"):
            try:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise RuntimeError("重排前时间预算已耗尽。")
                hits, reranked = self.models.rerank(query, hits, timeout=remaining)
            except RuntimeError:
                warning = "重排模型不可用，已保留原检索顺序。"
        result = {"hits": hits, "mode": actual_mode, "reranked": reranked}
        if actual_mode == "bm25" and mode != "bm25":
            result["warning"] = "嵌入不可用或资料尚无向量，已回退 BM25。"
        elif warning:
            result["warning"] = warning
        return result
