"""Idempotent local corpus parser. Run with: python -m app.ingest [--rebuild]."""
import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"


def parse_pdf(path: Path):
    doc = fitz.open(path)
    chunks = []
    for page_no, page in enumerate(doc, 1):
        raw = page.get_text()
        # Keep clause paragraph and its subclauses together; do not split a rule
        # from conditions located within the same paragraph.
        matches = list(re.finditer(r"(?m)^\s*(\d+\.\d+(?:\.\d+)?)\s+", raw))
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            section = re.sub(r"\s+", " ", raw[match.start():end]).strip()
            if len(section) < 35:
                continue
            cid = match.group(1)
            chunks.append({"id": f"CAR-{cid}" if "dgca_car" in path.stem else f"{path.stem}-{cid}", "clause": cid, "page": page_no,
                           "source": path.name, "source_url": "https://www.dgca.gov.in/digigov-portal/Upload?attachId=we1PSlOuQhYdHcwKKrm7ew%3D%3D&flag=iframeAttachView" if "dgca_car" in path.stem else "https://www.airindia.com/content/dam/air-india/pdfs/passenger-charter-know-your-rights.pdf", "text": section,
                           "tags": sorted(set(re.findall(r"[a-z]{4,}", section.lower())))[:30]})
    return chunks


def build(rebuild=False):
    started = time.monotonic()
    chunks = []
    sources = []
    for path in sorted(CORPUS.glob("*.pdf")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sources.append({"file": path.name, "sha256": digest, "bytes": path.stat().st_size})
        chunks.extend(parse_pdf(path))
    if not chunks:
        raise SystemExit("No text-bearing PDF documents found under corpus/")
    curated_data = json.loads((CORPUS / "dgca_car.json").read_text(encoding="utf8"))
    curated = curated_data["chunks"]
    local_names = {s["file"] for s in sources}
    sources.extend(s for s in curated_data.get("sources", []) if s.get("file") not in local_names)
    known = {c["id"] for c in curated}
    chunks = curated + [c for c in chunks if c["id"] not in known]
    out = {"source": "DGCA CAR Rev. 4 and Air India Passenger Charter", "source_url": "https://www.dgca.gov.in/digigov-portal/Upload?attachId=we1PSlOuQhYdHcwKKrm7ew%3D%3D&flag=iframeAttachView", "built_at": datetime.now(timezone.utc).isoformat(),
           "sources": sources, "chunks": chunks}
    target = CORPUS / "parsed_corpus.json"
    # Content-addressed input means repeating the same ingest is a no-op.
    if target.exists() and not rebuild:
        previous = json.loads(target.read_text(encoding="utf8"))
        if previous.get("sources") == sources:
            print(json.dumps({"status": "unchanged", "chunks": len(previous["chunks"]), "sources": sources}))
            return
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf8")
    (CORPUS / "dgca_car.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf8")
    elapsed = time.monotonic() - started
    manifest = {"built_at": out["built_at"], "sources": sources, "chunk_count": len(out["chunks"]),
                "ingest_seconds": round(elapsed, 3), "chunks_per_second": round(len(out["chunks"])/max(elapsed, .001), 2)}
    (CORPUS / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    print(json.dumps({"status": "built", **manifest}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    build(parser.parse_args().rebuild)
