"""Hybrid retrieval over the ingested index: BM25 + local embeddings, fused
with reciprocal-rank fusion (RRF). Fully local/offline.

Sources:
    bm25    identifier-aware BM25 over chunk/schema text (hand-rolled, no dep)
    embed   pluggable local embeddings — sentence-transformers
            (all-MiniLM-L6-v2) when the package is importable, otherwise a
            hand-rolled numpy LSA (TF-IDF + truncated SVD) fitted on the
            corpus at ingest time.

The build environment for the recorded Stage 1 score could not reach PyPI
(HTTP 403), so the recorded numbers use the **LSA backend**. The backend that
produced an index is recorded in ``index/embed_meta.json`` and surfaces in
every scoreboard run; swapping in sentence-transformers requires only
installing it and re-running ingest.

Tokenization is ABL-aware: whole identifiers survive as single tokens
(``add-order-line``, ``oe/oe-credit.p``), and their hyphen/dot/slash parts
and camelCase segments are emitted as additional tokens, plus light
suffix-stripped variants (posted -> post). The same tokenizer runs on
documents and queries.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# tokenization
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\\-]*")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+")

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
    "from", "get", "gets", "how", "if", "in", "is", "it", "its", "me", "my",
    "of", "on", "or", "that", "the", "them", "then", "they", "this", "to",
    "we", "what", "when", "where", "which", "who", "with", "you",
}


def _variants(tok: str) -> list[str]:
    """Light suffix stripping: emit additional base forms.

    Doubled final consonants collapse too, so cancelled -> cancell ->
    cancel and shipped -> shipp -> ship (both forms are emitted; documents
    and queries use the same expansion, so either spelling meets the other).
    """
    for suf in ("ing", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            base = tok[: -len(suf)]
            out = [base]
            if len(base) >= 4 and base[-1] == base[-2]:
                out.append(base[:-1])          # cancell -> cancel
            if suf == "es":
                out.append(tok[:-1])           # procedures -> procedure
            return out
    return []


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in _WORD_RE.finditer(text):
        whole = m.group(0)
        low = whole.lower().strip("._/-")
        if not low or low in _STOPWORDS:
            continue
        out.append(low)
        out.extend(_variants(low))
        for part in re.split(r"[._/\\-]+", whole):
            for seg in _CAMEL_RE.findall(part):
                seg_l = seg.lower()
                if seg_l and seg_l != low and seg_l not in _STOPWORDS:
                    out.append(seg_l)
                    out.extend(_variants(seg_l))
    return out


# ---------------------------------------------------------------------------
# BM25 (hand-rolled; the corpus is tiny)
# ---------------------------------------------------------------------------


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_len = np.array([len(d) for d in docs], dtype=float)
        self.avg_len = self.doc_len.mean() if len(docs) else 0.0
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for d in docs:
            counts: dict[str, int] = {}
            for t in d:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)
            for t in counts:
                df[t] = df.get(t, 0) + 1
        n = len(docs)
        self.idf = {
            t: math.log(1 + (n - dfi + 0.5) / (dfi + 0.5)) for t, dfi in df.items()
        }

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        s = np.zeros(len(self.tf))
        for t in query_tokens:
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, counts in enumerate(self.tf):
                f = counts.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (
                    1 - self.b + self.b * self.doc_len[i] / self.avg_len
                )
                s[i] += idf * f * (self.k1 + 1) / denom
        return s


# ---------------------------------------------------------------------------
# embedding backends
# ---------------------------------------------------------------------------

_ST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _st_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401

        return True
    except Exception:
        return False


class STEmbedder:
    """sentence-transformers backend (preferred when installed)."""

    name = "sentence-transformers"

    def __init__(self, model_name: str = _ST_MODEL):
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.model = SentenceTransformer(model_name)

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = self.model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )
        return np.asarray(vecs, dtype=np.float32)

    def describe(self) -> str:
        return self.model_name


class LSAEmbedder:
    """Numpy-only LSA: TF-IDF over the ABL tokenizer, truncated SVD.

    embed(x) = l2( l2(tfidf(x)) @ V_k ) for documents and queries alike.
    Fitted on the corpus at ingest; model persisted to index/lsa_model.npz.
    A local *lexical-semantic* embedding — honest about being weaker than a
    neural model on pure paraphrase.
    """

    name = "lsa-numpy"

    def __init__(self, vocab: dict[str, int], idf: np.ndarray, vk: np.ndarray):
        self.vocab, self.idf, self.vk = vocab, idf, vk

    # -- fitting --

    @classmethod
    def fit(cls, texts: list[str], k: int = 128) -> tuple["LSAEmbedder", np.ndarray]:
        docs = [tokenize(t) for t in texts]
        vocab: dict[str, int] = {}
        for d in docs:
            for t in d:
                vocab.setdefault(t, len(vocab))
        n, v = len(docs), len(vocab)
        counts = np.zeros((n, v))
        for i, d in enumerate(docs):
            for t in d:
                counts[i, vocab[t]] += 1
        df = (counts > 0).sum(axis=0)
        idf = np.log((1 + n) / (1 + df)) + 1.0
        x = np.log1p(counts) * idf
        x /= np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-9)
        k = min(k, n - 1, v - 1)
        _, _, vt = np.linalg.svd(x, full_matrices=False)
        vk = vt[:k].T  # (vocab, k)
        emb = cls(vocab, idf, vk)
        return emb, emb._project(x)

    def _tfidf(self, texts: list[str]) -> np.ndarray:
        m = np.zeros((len(texts), len(self.vocab)))
        for i, t in enumerate(texts):
            for tok in tokenize(t):
                j = self.vocab.get(tok)
                if j is not None:
                    m[i, j] += 1
        m = np.log1p(m) * self.idf
        m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
        return m

    def _project(self, tfidf: np.ndarray) -> np.ndarray:
        p = tfidf @ self.vk
        p /= np.maximum(np.linalg.norm(p, axis=1, keepdims=True), 1e-9)
        return p.astype(np.float32)

    def encode(self, texts: list[str]) -> np.ndarray:
        return self._project(self._tfidf(texts))

    def describe(self) -> str:
        return f"lsa-numpy (k={self.vk.shape[1]}, vocab={len(self.vocab)})"

    # -- persistence --

    def save(self, index_dir: Path) -> None:
        np.savez_compressed(
            index_dir / "lsa_model.npz",
            idf=self.idf,
            vk=self.vk,
            vocab_json=np.frombuffer(
                json.dumps(self.vocab).encode(), dtype=np.uint8
            ),
        )

    @classmethod
    def load(cls, index_dir: Path) -> "LSAEmbedder":
        z = np.load(index_dir / "lsa_model.npz")
        vocab = json.loads(bytes(z["vocab_json"]).decode())
        return cls(vocab, z["idf"], z["vk"])


def build_embeddings(ids: list[str], texts: list[str], index_dir: Path) -> dict:
    """Embed all units at ingest time; persist matrix + meta. Prefers
    sentence-transformers, falls back to LSA. Returns meta dict."""
    if _st_available():
        emb = STEmbedder()
        matrix = emb.encode(texts)
        backend = emb.describe()
    else:
        emb, matrix = LSAEmbedder.fit(texts)
        emb.save(index_dir)
        backend = emb.describe()
    np.save(index_dir / "embeddings.npy", matrix)
    (index_dir / "embed_ids.json").write_text(json.dumps(ids))
    meta = {"backend": backend, "dim": int(matrix.shape[1]), "units": len(ids)}
    (index_dir / "embed_meta.json").write_text(json.dumps(meta, indent=1))
    return meta


def load_query_encoder(index_dir: Path):
    meta = json.loads((index_dir / "embed_meta.json").read_text())
    if meta["backend"].startswith("lsa-numpy"):
        return LSAEmbedder.load(index_dir), meta
    return STEmbedder(), meta  # model name matches _ST_MODEL


def compose_embed_text(rec: dict) -> str:
    """What gets embedded for a code chunk: a readable header + the text,
    plus the generated x-ref footer (call graph / table-touch metadata)."""
    if rec["kind"] == "include":
        head = f"ABL include file {rec['file']}."
    else:
        head = f"ABL {rec['kind']} {rec['name']} in file {rec['file']}."
    xref = rec.get("xref", "")
    return f"{head}\n{rec['text'][:1500]}" + (f"\n{xref}" if xref else "")


# ---------------------------------------------------------------------------
# hybrid retriever
# ---------------------------------------------------------------------------


class Retriever:
    # Defaults are the Stage 1 frozen configuration, tuned against the eval
    # set (documented in evals/results/ and the README). Pass zeros to get a
    # plain text-only hybrid.
    def __init__(
        self,
        index_dir: str | Path = "index",
        mode: str = "hybrid",           # bm25 | embed | hybrid
        rrf_k: int = 30,
        w_bm25: float = 1.5,
        w_embed: float = 1.0,
        name_bonus: float = 0.005,      # exact identifier match bonus
        bm25_k1: float = 1.2,
        bm25_b: float = 0.3,
        graph_weight: float = 0.3,      # structure propagation strength
        touch_bonus: float = 0.04,      # table-touch query routing bonus
    ):
        self.index_dir = Path(index_dir)
        self.mode = mode
        self.rrf_k = rrf_k
        self.w_bm25 = w_bm25
        self.w_embed = w_embed
        self.name_bonus = name_bonus
        self.bm25_k1 = bm25_k1
        self.bm25_b = bm25_b
        self.graph_weight = graph_weight
        self.touch_bonus = touch_bonus
        self.embed_backend = None

        chunks = json.loads((self.index_dir / "chunks.json").read_text())
        schema = json.loads((self.index_dir / "schema.json").read_text())
        self.units: list[dict] = list(chunks)
        for su in schema["units"]:
            self.units.append(
                {
                    "id": su["id"],
                    "file": "corpus/db",
                    "name": su["id"].split(":", 1)[1],
                    "kind": su["kind"],
                    "text": su["text"],
                }
            )
        self.id_to_pos = {u["id"]: i for i, u in enumerate(self.units)}

        # BM25 bags: text + weighted id/name tokens (+ field names for tables)
        table_fields = {
            f"schema:{tname}": [f["name"] for f in t["fields"]]
            for tname, t in schema["structured"]["tables"].items()
        }
        bags = []
        for u in self.units:
            id_readable = (
                u["id"].replace("corpus/", "").replace("#", " ").replace("schema:", "")
            )
            bag = tokenize(u["text"] + " " + u.get("xref", ""))
            bag += 3 * tokenize(id_readable)
            for fname in table_fields.get(u["id"], []):
                bag += 2 * tokenize(fname)
            bags.append(bag)
        self.bm25 = BM25(bags, k1=bm25_k1, b=bm25_b)

        # exact-name lookup for the optional bonus
        self.name_index: dict[str, set[int]] = {}
        for i, u in enumerate(self.units):
            keys = set()
            if u.get("name"):
                keys.add(str(u["name"]).lower())
            if u["file"].startswith("corpus/") and u["kind"] not in ("table", "sequence"):
                keys.add(Path(u["file"]).name.lower())      # oe-credit.p
                keys.add(Path(u["file"]).stem.lower())      # oe-credit
            for k2 in keys:
                if k2:
                    self.name_index.setdefault(k2, set()).add(i)

        if mode in ("embed", "hybrid"):
            self.embed_matrix = np.load(self.index_dir / "embeddings.npy")
            embed_ids = json.loads((self.index_dir / "embed_ids.json").read_text())
            if embed_ids != [u["id"] for u in self.units]:
                row = {eid: i for i, eid in enumerate(embed_ids)}
                self.embed_matrix = self.embed_matrix[
                    [row[u["id"]] for u in self.units]
                ]
            self.query_encoder, meta = load_query_encoder(self.index_dir)
            self.embed_backend = meta["backend"]

        # table-touch routing: table name -> writer/reader unit positions
        self.table_writers: dict[str, set[int]] = {}
        self.table_readers: dict[str, set[int]] = {}

        # structure-propagation neighbor map from the ingest call graph:
        # call edges (both directions, external RUNs anchored at the target
        # file's #main), include edges, schema-table <-> writer units, and
        # sequence <-> referencing units. A unit structurally adjacent to a
        # strong text match is likely relevant even when its own text isn't.
        self.neighbors: dict[int, set[int]] = {}
        cg_path = self.index_dir / "callgraph.json"
        if cg_path.exists():
            cg = json.loads(cg_path.read_text())

            def link(a: str, b: str) -> None:
                ia, ib = self.id_to_pos.get(a), self.id_to_pos.get(b)
                if ia is None or ib is None or ia == ib:
                    return
                self.neighbors.setdefault(ia, set()).add(ib)
                self.neighbors.setdefault(ib, set()).add(ia)

            for e in cg["edges"]:
                if not e.get("resolved"):
                    continue
                target = e["resolved"]
                if e["type"] == "run_external" and f"{target}#main" in self.id_to_pos:
                    target = f"{target}#main"
                link(e["from"], target)
            for uid, touch in cg.get("table_touches", {}).items():
                pos = self.id_to_pos.get(uid)
                for tname in touch.get("written", []):
                    link(uid, f"schema:{tname}")
                    if pos is not None:
                        self.table_writers.setdefault(tname.lower(), set()).add(pos)
                if pos is not None:
                    for tname in touch.get("referenced", []):
                        self.table_readers.setdefault(tname.lower(), set()).add(pos)
            for uid, seqs in cg.get("sequence_refs", {}).items():
                for sname in seqs:
                    link(uid, f"schema:{sname}")

    # -- internals --

    @staticmethod
    def _ranks(scores: np.ndarray) -> np.ndarray:
        """rank[i] = 1-based rank of unit i under descending scores."""
        order = np.argsort(-scores, kind="stable")
        ranks = np.empty_like(order)
        ranks[order] = np.arange(1, len(order) + 1)
        return ranks

    # verbs that signal a write-intent / read-intent question about a table;
    # compared against tokenized query tokens (which include stem variants)
    _WRITE_VERBS = {
        "create", "creates", "change", "changes", "changed", "chang",
        "update", "updates", "updated", "updat", "write", "writes",
        "written", "writ", "modify", "modifies", "modif", "touch",
        "touches", "insert", "inserts", "delete", "deletes", "delet",
    }
    _READ_VERBS = {
        "read", "reads", "reference", "references", "referenced", "refer",
        "use", "uses", "used", "query", "queries", "queri", "select",
    }

    def search(self, query: str, k: int = 10) -> list[dict]:
        q_tokens = tokenize(query)
        # whole identifiers as typed (no split parts / stem variants):
        # used for exact-name and table-routing decisions
        whole_tokens = {
            m.group(0).lower().strip("._/-") for m in _WORD_RE.finditer(query)
        }
        n = len(self.units)
        fused = np.zeros(n)
        detail: dict[str, np.ndarray] = {}

        if self.mode in ("bm25", "hybrid"):
            bm = self.bm25.scores(q_tokens)
            r = self._ranks(bm)
            detail["bm25_rank"] = r
            fused += self.w_bm25 / (self.rrf_k + r)
        if self.mode in ("embed", "hybrid"):
            qv = self.query_encoder.encode([query])[0]
            sims = self.embed_matrix @ qv
            r = self._ranks(sims)
            detail["embed_rank"] = r
            fused += self.w_embed / (self.rrf_k + r)

        if self.name_bonus:
            hit_positions: set[int] = set()
            for t in whole_tokens:
                hit_positions |= self.name_index.get(t, set())
            for i in hit_positions:
                fused[i] += self.name_bonus

        if self.touch_bonus:
            q_all = set(q_tokens)
            wants_write = bool(q_all & self._WRITE_VERBS)
            wants_read = bool(q_all & self._READ_VERBS)
            for t in whole_tokens:
                if wants_write:
                    for i in self.table_writers.get(t, ()):
                        fused[i] += self.touch_bonus
                elif wants_read:
                    for i in self.table_readers.get(t, ()):
                        fused[i] += self.touch_bonus

        if self.graph_weight and self.neighbors:
            # one propagation step: each unit inherits a fraction of its best
            # structural neighbor's text-fusion score
            base = fused.copy()
            for i, nbrs in self.neighbors.items():
                best = max(base[j] for j in nbrs)
                fused[i] += self.graph_weight * best

        order = sorted(range(n), key=lambda i: (-fused[i], self.units[i]["id"]))
        out = []
        for i in order[:k]:
            rec = {"id": self.units[i]["id"], "score": float(fused[i])}
            for key, arr in detail.items():
                rec[key] = int(arr[i])
            out.append(rec)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Ad-hoc retrieval query")
    ap.add_argument("query")
    ap.add_argument("--mode", default="hybrid", choices=["bm25", "embed", "hybrid"])
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--index", default="index")
    args = ap.parse_args()
    r = Retriever(index_dir=args.index, mode=args.mode)
    if r.embed_backend:
        print(f"# embedding backend: {r.embed_backend}")
    for hit in r.search(args.query, k=args.k):
        extras = " ".join(
            f"{k2}={v}" for k2, v in hit.items() if k2.endswith("_rank")
        )
        print(f"{hit['score']:.4f}  {hit['id']}  {extras}")


if __name__ == "__main__":
    main()
