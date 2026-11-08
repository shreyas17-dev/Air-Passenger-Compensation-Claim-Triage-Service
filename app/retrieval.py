"""Local TF-IDF embeddings in a LangChain FAISS store; index is rebuildable."""
import argparse
import json
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_community.vectorstores import FAISS
from sklearn.feature_extraction.text import TfidfVectorizer

ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = ROOT / "corpus" / "faiss_index"
SOURCE = ROOT / "corpus" / "dgca_car.json"


class LocalTfidfEmbeddings(Embeddings):
    """Sparse lexical-semantic representation; no hosted model or credentials."""
    def __init__(self, vectorizer):
        self.vectorizer = vectorizer

    def embed_documents(self, texts):
        return self.vectorizer.transform(texts).toarray().tolist()

    def embed_query(self, text):
        return self.vectorizer.transform([text]).toarray()[0].tolist()


def build_index(rebuild=False):
    data = json.loads(SOURCE.read_text(encoding="utf8"))
    docs = [Document(page_content=p["text"], metadata={k: v for k, v in p.items() if k != "text"}) for p in data["chunks"]]
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), max_features=12000, norm="l2")
    vectorizer.fit([d.page_content for d in docs])
    store = FAISS.from_documents(docs, LocalTfidfEmbeddings(vectorizer))
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    store.save_local(str(INDEX_DIR))
    import pickle
    with (INDEX_DIR / "vectorizer.pkl").open("wb") as f:
        pickle.dump(vectorizer, f)
    manifest = {"chunk_count": len(docs), "source": data.get("source"), "index": "FAISS",
                "embedding": "local TF-IDF unigram/bigram", "rebuildable": True}
    (INDEX_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    return manifest


def load_index():
    if not (INDEX_DIR / "index.faiss").exists():
        return None
    import pickle
    with (INDEX_DIR / "vectorizer.pkl").open("rb") as f:
        vectorizer = pickle.load(f)
    return FAISS.load_local(str(INDEX_DIR), LocalTfidfEmbeddings(vectorizer), allow_dangerous_deserialization=True)


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
    parser.parse_args()
    print(json.dumps(build_index(True), indent=2))
