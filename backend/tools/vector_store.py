"""Dense retrieval over the lakehouse's series indexes, for the half of
discovery that lexical matching cannot reach.

Lexical scoring fails in a specific, reproducible way: it can only find a
series whose *spelling* the question shares. BDDK abbreviates -- the consumer
NPL line is published as `Takipteki Tüketici Krd.`, so the word "kredileri"
never appears in it, and three sibling rows that do spell it in full
(`Takipteki Konut/Taşıt/İhtiyaç Kredileri`) outranked the exact answer. No
alias table closes that class of gap in general: the next corpus refresh
invents its own abbreviation.

An embedding does, because "Tüketici Krd." and "tüketici kredileri" are near
each other in vector space regardless of how either is spelled. This module is
the dense half; `lakehouse.discover` fuses the two with Reciprocal Rank Fusion.

Three constraints shaped the design:

- **The brief names LanceDB**, so that is the backend when it is installed.
  The fallback is a NumPy matrix plus a JSON sidecar under `data/vectors/`,
  which is the same data in a form that needs no dependency -- a clone that
  never ran `pip install ".[vector]"` still gets dense retrieval once an index
  has been built, and a clone with neither still gets lexical ranking.
- **Embeddings go through Kloudeks** (`llm.client.KloudeksClient.embed`), like
  every other model call in this codebase. No vendor SDK, no hosted service.
- **Everything here is optional.** `available()` is false when no index has
  been built, and every caller falls back to lexical ranking rather than
  failing. Discovery is the tool the whole agent depends on; it must not
  acquire a hard dependency on a network call.

The index is *not* built by `lakehouse.build`: it needs an embedding endpoint,
and the build is required to run offline on a bare clone. Build it explicitly
with `python -m backend.tools.vector_store --build`.
"""
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..core.config import DATA_DIR
from ..core.labels import ascii_fold

VECTOR_DIR = DATA_DIR / "vectors"
VECTORS_PATH = VECTOR_DIR / "series.npy"
METADATA_PATH = VECTOR_DIR / "series.json"
LANCE_PATH = VECTOR_DIR / "lance"
LANCE_TABLE = "series"

# The fields that make a series findable by meaning rather than by spelling.
# `grain` is in here deliberately: "aylik" in a question should pull monthly
# series toward the top on the dense side too, not rely solely on the
# deterministic gate in `lakehouse._apply_grain_policy`.
def candidate_text(candidate: Dict[str, Any]) -> str:
    """The string a candidate is indexed and searched by."""
    return (
        f"{candidate.get('name') or ''} "
        f"{candidate.get('dataset') or ''} "
        f"{candidate.get('source') or ''} "
        f"grain:{candidate.get('grain') or 'unknown'}"
    ).strip()


Embedder = Callable[[List[str]], List[List[float]]]


def kloudeks_embedder(client=None) -> Optional[Embedder]:
    """An embedder backed by Kloudeks, or None when none is configured."""
    if client is None:
        try:
            from ..core.config import kloudeks_api_key
            from ..llm import KloudeksClient
        except ImportError:                                  # pragma: no cover - import guard
            return None
        if not kloudeks_api_key():
            return None
        client = KloudeksClient()
    if not hasattr(client, "embed"):
        return None

    def embed(texts: List[str]) -> List[List[float]]:
        return client.embed(texts)

    return embed


# --------------------------------------------------------------------------
# similarity


def _l2_normalise(vector: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, defined as 0.0 for a zero vector rather than NaN."""
    if len(left) != len(right):
        raise ValueError(f"dimension mismatch: {len(left)} vs {len(right)}")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


@dataclass
class VectorHit:
    """One dense-retrieval result: the indexed metadata plus its similarity."""

    key: str
    source: str
    dataset: Optional[str]
    name: str
    grain: Optional[str]
    similarity: float

    def identity(self) -> Tuple[str, str, str]:
        """Matches `lakehouse._identity`: a key alone is not unique across datasets."""
        return (str(self.source), str(self.dataset), str(self.key))

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "source": self.source, "dataset": self.dataset,
                "name": self.name, "grain": self.grain,
                "similarity": round(self.similarity, 6)}


class VectorStore:
    """Disk-backed dense index over the series catalogue.

    Prefers LanceDB when it is installed (the brief names it); otherwise a
    NumPy matrix beside a JSON sidecar. Both hold L2-normalised vectors, so
    cosine similarity is a dot product either way and the two backends return
    the same ranking for the same index.
    """

    def __init__(self, directory: Path = VECTOR_DIR, embedder: Optional[Embedder] = None,
                 prefer_lance: bool = True):
        self.directory = Path(directory)
        self.embedder = embedder
        self.prefer_lance = prefer_lance
        self._vectors: Optional[List[List[float]]] = None
        self._metadata: Optional[List[Dict[str, Any]]] = None
        self._backend: Optional[str] = None

    # -- backend detection --------------------------------------------------

    @staticmethod
    def _lancedb():
        try:
            import lancedb                                   # noqa: F401 -- probe only
            return lancedb
        except ImportError:
            return None

    def _lance_table(self):
        lancedb = self._lancedb() if self.prefer_lance else None
        if lancedb is None or not (self.directory / "lance").exists():
            return None
        try:
            connection = lancedb.connect(str(self.directory / "lance"))
            if LANCE_TABLE not in connection.table_names():
                return None
            return connection.open_table(LANCE_TABLE)
        except Exception:                                    # noqa: BLE001 -- a broken index is "no index"
            return None

    # -- build --------------------------------------------------------------

    def build(self, candidates: List[Dict[str, Any]], embedder: Optional[Embedder] = None,
              batch_size: int = 64) -> Dict[str, Any]:
        """Embed every candidate and write the index. Returns a small report."""
        embedder = embedder or self.embedder or kloudeks_embedder()
        if embedder is None:
            raise RuntimeError(
                "no embedder: set KLOUDEKS_API_KEY or pass one explicitly. "
                "Dense retrieval is optional -- discovery falls back to lexical ranking.")
        if not candidates:
            raise ValueError("nothing to index")

        texts = [candidate_text(candidate) for candidate in candidates]
        vectors: List[List[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            vectors.extend(_l2_normalise(vector) for vector in embedder(batch))
        if len(vectors) != len(candidates):
            raise RuntimeError(f"embedder returned {len(vectors)} vectors for {len(texts)} texts")

        metadata = [{"key": str(c.get("key")), "source": str(c.get("source")),
                     "dataset": c.get("dataset"), "name": c.get("name"),
                     "grain": c.get("grain"), "text": text}
                    for c, text in zip(candidates, texts)]

        self.directory.mkdir(parents=True, exist_ok=True)
        backend = self._write_lance(vectors, metadata) or self._write_numpy(vectors, metadata)
        self._vectors, self._metadata, self._backend = vectors, metadata, backend
        return {"n_indexed": len(vectors), "dimensions": len(vectors[0]),
                "backend": backend, "directory": str(self.directory)}

    def _write_lance(self, vectors, metadata) -> Optional[str]:
        lancedb = self._lancedb() if self.prefer_lance else None
        if lancedb is None:
            return None
        try:
            connection = lancedb.connect(str(self.directory / "lance"))
            rows = [dict(meta, vector=vector) for meta, vector in zip(metadata, vectors)]
            connection.create_table(LANCE_TABLE, data=rows, mode="overwrite")
            return "lancedb"
        except Exception:                                    # noqa: BLE001 -- fall back to NumPy
            return None

    def _write_numpy(self, vectors, metadata) -> str:
        try:
            import numpy as np
            np.save(VECTORS_PATH if self.directory == VECTOR_DIR
                    else self.directory / "series.npy", np.asarray(vectors, dtype="float32"))
            payload = {"format": "numpy", "dimensions": len(vectors[0]), "rows": metadata}
        except ImportError:
            # No NumPy either: keep the vectors in the JSON sidecar. Slower to
            # load, identical results, and it keeps the store usable anywhere.
            payload = {"format": "json", "dimensions": len(vectors[0]),
                       "rows": [dict(meta, vector=vector) for meta, vector in zip(metadata, vectors)]}
        (self.directory / "series.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return "numpy" if payload["format"] == "numpy" else "json"

    # -- load ---------------------------------------------------------------

    def _load(self) -> bool:
        """Populate the in-memory index. False when there is nothing on disk."""
        if self._vectors is not None:
            return True
        table = self._lance_table()
        if table is not None:
            try:
                rows = table.to_pandas().to_dict("records")
                self._vectors = [list(row["vector"]) for row in rows]
                self._metadata = [{k: row.get(k) for k in ("key", "source", "dataset", "name", "grain", "text")}
                                  for row in rows]
                self._backend = "lancedb"
                return True
            except Exception:                                # noqa: BLE001 -- fall through to NumPy
                pass

        sidecar = self.directory / "series.json"
        if not sidecar.exists():
            return False
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        rows = payload.get("rows") or []
        if payload.get("format") == "json":
            self._vectors = [list(row.get("vector") or []) for row in rows]
            self._metadata = [{k: row.get(k) for k in ("key", "source", "dataset", "name", "grain", "text")}
                              for row in rows]
            self._backend = "json"
            return bool(self._vectors)
        matrix_path = self.directory / "series.npy"
        if not matrix_path.exists():
            return False
        try:
            import numpy as np
            self._vectors = [list(map(float, row)) for row in np.load(matrix_path)]
        except (ImportError, OSError, ValueError):
            return False
        self._metadata = rows
        self._backend = "numpy"
        return bool(self._vectors)

    def available(self) -> bool:
        """True when an index exists on disk and can be read."""
        try:
            return self._load()
        except Exception:                                    # noqa: BLE001 -- never break discovery
            return False

    def backend(self) -> Optional[str]:
        return self._backend if self.available() else None

    def size(self) -> int:
        return len(self._vectors) if self.available() else 0

    # -- search -------------------------------------------------------------

    def search_vector(self, query_vector: Sequence[float], limit: int = 25) -> List[VectorHit]:
        """Top matches for an already-embedded query."""
        if not self.available() or not query_vector:
            return []
        query = _l2_normalise(query_vector)
        hits: List[VectorHit] = []
        for vector, meta in zip(self._vectors or [], self._metadata or []):
            if len(vector) != len(query):
                continue                                     # a stale index from another model
            hits.append(VectorHit(
                key=str(meta.get("key")), source=str(meta.get("source")),
                dataset=meta.get("dataset"), name=str(meta.get("name") or ""),
                grain=meta.get("grain"), similarity=cosine(query, vector)))
        hits.sort(key=lambda hit: -hit.similarity)
        return hits[:limit]

    def search(self, query: str, limit: int = 25,
               embedder: Optional[Embedder] = None) -> List[VectorHit]:
        """Top matches for a natural-language query. Empty list when unavailable."""
        if not self.available():
            return []
        embedder = embedder or self.embedder or kloudeks_embedder()
        if embedder is None:
            return []
        try:
            vectors = embedder([ascii_fold(query)])
        except Exception:                                    # noqa: BLE001 -- a model outage is not an error here
            return []
        if not vectors:
            return []
        return self.search_vector(vectors[0], limit=limit)


_DEFAULT_STORE: Optional[VectorStore] = None


def default_store() -> VectorStore:
    """The process-wide store over `data/vectors/`, loaded at most once."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = VectorStore()
    return _DEFAULT_STORE


def reset_default_store() -> None:
    """Drop the cached store. Tests that build an index call this."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = None


# --------------------------------------------------------------------------
# Reciprocal Rank Fusion


RRF_K = 60


def reciprocal_rank_fusion(rankings: Sequence[Sequence[Any]], k: Optional[int] = None,
                           key: Callable[[Any], Any] = lambda item: item,
                           weights: Optional[Sequence[float]] = None) -> Dict[Any, float]:
    """Fuse several ranked lists into one score per identity.

        score(d) = sum over lists of 1 / (k + rank(d)),  rank starting at 1

    Rank-based rather than score-based on purpose: the lexical scorer returns
    unbounded sums and the dense side returns cosines in [-1, 1], so there is
    no honest way to add them directly. RRF only needs the orderings to be
    meaningful, which is exactly what each half guarantees about itself. k=60
    is the constant from the original paper; it damps the top of each list
    enough that one confident-but-wrong ranker cannot dominate the other.
    """
    # Read at call time, not bound as a default: `RRF_K` is a tuning knob and a
    # default argument would freeze it at import, which silently ignores every
    # attempt to change it.
    k = RRF_K if k is None else k
    fused: Dict[Any, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking, start=1):
            identity = key(item)
            fused[identity] = fused.get(identity, 0.0) + 1.0 / (k + position)
    return fused


# --------------------------------------------------------------------------
# CLI


def _catalogue() -> List[Dict[str, Any]]:
    """Every indexable series, in the candidate shape `discover` produces."""
    from .lakehouse import catalogue_candidates
    return catalogue_candidates()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build or query the series vector index.")
    parser.add_argument("--build", action="store_true", help="embed the catalogue and write the index")
    parser.add_argument("--search", metavar="QUERY", help="query the existing index")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--status", action="store_true", help="report what is on disk")
    args = parser.parse_args(argv)

    store = default_store()
    if args.build:
        candidates = _catalogue()
        print(f"Embedding {len(candidates)} series...")
        report = store.build(candidates)
        print(f"  {report['n_indexed']} vectors, {report['dimensions']} dims, "
              f"backend={report['backend']}, at {report['directory']}")
        return 0
    if args.search:
        hits = store.search(args.search, limit=args.limit)
        if not hits:
            print("no hits (index missing, or no embedder configured)")
            return 1
        for hit in hits:
            print(f"  {hit.similarity:6.3f}  {hit.source:9} {hit.key[:48]:50} {hit.name[:44]}")
        return 0

    reset_default_store()
    store = default_store()
    print(f"index available: {store.available()}")
    print(f"backend        : {store.backend()}")
    print(f"series indexed : {store.size()}")
    print(f"lancedb        : {'installed' if VectorStore._lancedb() else 'not installed'}")
    print(f"embedder       : {'configured' if kloudeks_embedder() else 'not configured'}")
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
