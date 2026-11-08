"""Fixed-size local n-gram embeddings in a LangChain FAISS store."""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_community.vectorstores import FAISS
from sklearn.feature_extraction.text import HashingVectorizer

ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = ROOT / "corpus" / "faiss_index"
SOURCE = ROOT / "corpus" / "dgca_car.json"
FEATURES = 1 << 14


class LocalHashedNgramEmbeddings(Embeddings):
    """Fixed-size local n-gram vectors that support append-only indexing."""
    def __init__(self):
        self.vectorizer = HashingVectorizer(stop_words="english", ngram_range=(1, 2),
                                            n_features=FEATURES, alternate_sign=False,
                                            norm="l2", binary=False)

    def embed_documents(self, texts):
        return self.vectorizer.transform(texts).toarray().tolist()

    def embed_query(self, text):
        return self.vectorizer.transform([text]).toarray()[0].tolist()


def _embedding():
    return LocalHashedNgramEmbeddings()


def _existing_docs(store):
    return {doc.metadata.get("id"): doc for doc in store.docstore._dict.values()
            if doc.metadata.get("id")}


def build_index(rebuild=False, append=False):
    started = time.monotonic()
    data = json.loads(SOURCE.read_text(encoding="utf8"))
    docs = [Document(page_content=p["text"], metadata={k: v for k, v in p.items() if k != "text"}) for p in data["chunks"]]
    embeddings = _embedding()
    source_digest = hashlib.sha256(json.dumps(data["chunks"], sort_keys=True, ensure_ascii=False).encode("utf8")).hexdigest()
    if append and INDEX_DIR.joinpath("index.faiss").exists():
        store = FAISS.load_local(str(INDEX_DIR), embeddings, allow_dangerous_deserialization=True)
        old_docs = _existing_docs(store)
        incoming = {doc.metadata["id"]: doc for doc in docs}
        changed = [doc_id for doc_id in old_docs.keys() & incoming.keys()
                   if old_docs[doc_id].page_content != incoming[doc_id].page_content]
        removed = old_docs.keys() - incoming.keys()
        if changed or removed:
            raise ValueError("Append mode only adds new passages; use --rebuild after editing or removing existing passages")
        additions = [doc for doc in docs if doc.metadata["id"] not in old_docs]
        if additions:
            store.add_documents(additions, ids=[doc.metadata["id"] for doc in additions])
        added = len(additions)
    elif not rebuild and INDEX_DIR.joinpath("index.faiss").exists():
        try:
            old_manifest = json.loads((INDEX_DIR / "manifest.json").read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            old_manifest = {}
        if old_manifest.get("source_sha256") == source_digest:
            return {**old_manifest, "status": "unchanged", "added_chunks": 0}
        store = FAISS.from_documents(docs, embeddings, ids=[doc.metadata["id"] for doc in docs])
        added = len(docs)
    else:
        store = FAISS.from_documents(docs, embeddings, ids=[doc.metadata["id"] for doc in docs])
        added = len(docs)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    store.save_local(str(INDEX_DIR))
    elapsed = time.monotonic()-started
    indexed_count = len(docs) if not append else len(_existing_docs(store))
    manifest = {"chunk_count": indexed_count,
                "added_chunks": added, "source": data.get("source"), "source_sha256": source_digest,
                "built_at": datetime.now(timezone.utc).isoformat(),
                "build_seconds": round(elapsed, 3),
                "vectors_per_second": round(added/max(elapsed, .001), 2),
                "index": "FAISS", "embedding": "local HashingVectorizer unigram/bigram (16384 fixed features)",
                "incremental_add_supported": True, "rebuildable": True}
    (INDEX_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    return manifest


def load_index():
    if not (INDEX_DIR / "index.faiss").exists():
        return None
    return FAISS.load_local(str(INDEX_DIR), _embedding(), allow_dangerous_deserialization=True)


def search(query, k=6):
    store = load_index()
    if store is None:
        return []
    matches = store.similarity_search_with_score(query, k=k)
    return [{**doc.metadata, "text": doc.page_content, "score": 1.0/(1.0+max(float(distance), 0.0))}
            for doc, distance in matches]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--append", action="store_true", help="Index newly added passage IDs without rebuilding the existing FAISS index")
    args = parser.parse_args()
    print(json.dumps(build_index(rebuild=args.rebuild, append=args.append), indent=2))
