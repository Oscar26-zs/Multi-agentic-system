"""Ingesta de documentos hacia el vector store.

Qué hace:
    Carga los documentos markdown de knowledge/, los divide en chunks y
    genera embeddings para almacenarlos en la base vectorial (rag/vector_store.py).

Responsabilidad dentro del sistema:
    Prepara la base de conocimiento interna (arquitectura, seguridad,
    desarrollo, testing) para que los retrievers especializados (rag/retrievers.py)
    puedan consultarla durante la ejecución del pipeline.

Decisiones (Fase 4 de Guia_Construccion.md):
    - Dominio de cada documento = nombre de su subcarpeta directa en knowledge/
      (architecture, security, development, testing). Se recorre con
      pathlib.Path.rglob en vez de un DirectoryLoader genérico para tener
      control explícito sobre ese metadato.
    - Chunking en dos pasadas: primero MarkdownHeaderTextSplitter (parte por
      headers #/##/### y adjunta el header como metadata), después
      RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=120) sobre
      las secciones que aún queden largas (ej. bloques de código/tablas).
    - Reindexado idempotente: se llama reset_collection() antes de volver a
      insertar, y cada chunk recibe un id determinístico (hash de
      source_path + índice + contenido). Reejecutar el script con el mismo
      contenido produce el mismo estado; con contenido editado, reconstruye
      la colección entera. Se prefirió sobre upsert incremental por ser más
      simple y confiable dado el volumen (7 archivos .md).
    - Ejecutable como script suelto (python rag/ingestion.py) para probarse
      de forma aislada antes de construir retrievers.py, según pide la guía.
"""

import hashlib
import sys
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

if __package__ in (None, ""):
    # Permite "python rag/ingestion.py" como script suelto (Fase 4 de la
    # guía): sin esto, sys.path solo contendría rag/, no la raíz del repo,
    # y "from rag.vector_store import ..." fallaría con ModuleNotFoundError.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.vector_store import get_vector_store, reset_collection

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

_HEADERS_TO_SPLIT_ON = [("#", "h1"), ("##", "h2"), ("###", "h3")]
_CHUNK_SIZE = 800
_CHUNK_OVERLAP = 120

__all__ = ["load_knowledge_documents", "chunk_documents", "ingest"]


def load_knowledge_documents(knowledge_dir: Path = KNOWLEDGE_DIR) -> list[Document]:
    """Lee cada .md de knowledge/<dominio>/ y lo envuelve como Document,
    con domain/source/source_path ya en metadata (antes de chunkear)."""
    documents: list[Document] = []
    for md_path in sorted(knowledge_dir.rglob("*.md")):
        domain = md_path.parent.name
        text = md_path.read_text(encoding="utf-8")
        documents.append(
            Document(
                page_content=text,
                metadata={
                    "domain": domain,
                    "source": md_path.name,
                    "source_path": str(md_path.relative_to(knowledge_dir.parent)).replace("\\", "/"),
                },
            )
        )
    return documents


def _most_specific_header(metadata: dict) -> str:
    return metadata.get("h3") or metadata.get("h2") or metadata.get("h1") or ""


def chunk_documents(documents: list[Document]) -> list[Document]:
    """Aplica MarkdownHeaderTextSplitter y luego RecursiveCharacterTextSplitter,
    devolviendo chunks con metadata: domain, source, source_path, header."""
    header_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=_HEADERS_TO_SPLIT_ON)
    char_splitter = RecursiveCharacterTextSplitter(
        chunk_size=_CHUNK_SIZE, chunk_overlap=_CHUNK_OVERLAP
    )

    chunks: list[Document] = []
    for doc in documents:
        sections = header_splitter.split_text(doc.page_content)
        for section in sections:
            for sub_chunk in char_splitter.split_text(section.page_content):
                chunks.append(
                    Document(
                        page_content=sub_chunk,
                        metadata={
                            "domain": doc.metadata["domain"],
                            "source": doc.metadata["source"],
                            "source_path": doc.metadata["source_path"],
                            "header": _most_specific_header(section.metadata),
                        },
                    )
                )
    return chunks


def _chunk_id(chunk: Document, index: int) -> str:
    raw = f"{chunk.metadata['source_path']}::{index}::{chunk.page_content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ingest() -> dict[str, int]:
    """Reindexa knowledge/ por completo. Devuelve conteo de chunks por dominio."""
    documents = load_knowledge_documents()
    chunks = chunk_documents(documents)
    ids = [_chunk_id(chunk, i) for i, chunk in enumerate(chunks)]

    reset_collection()
    store = get_vector_store()
    if chunks:
        store.add_documents(documents=chunks, ids=ids)

    counts: dict[str, int] = {}
    for chunk in chunks:
        domain = chunk.metadata["domain"]
        counts[domain] = counts.get(domain, 0) + 1
    return counts


if __name__ == "__main__":
    print("Fase 4 — smoke test de rag/ingestion.py")

    print(f"1. Buscando .md en {KNOWLEDGE_DIR}...")
    found_documents = load_knowledge_documents()
    if not found_documents:
        raise SystemExit(f"   ERROR - no se encontró ningún .md bajo {KNOWLEDGE_DIR}")
    by_domain: dict[str, int] = {}
    for doc in found_documents:
        by_domain[doc.metadata["domain"]] = by_domain.get(doc.metadata["domain"], 0) + 1
    for domain, count in sorted(by_domain.items()):
        print(f"   - {domain}: {count} archivo(s)")

    print("2. Chunkeando, generando embeddings y guardando en el vector store (reindexado completo)...")
    counts_by_domain = ingest()

    print("3. Resumen de chunks por dominio:")
    for domain, count in sorted(counts_by_domain.items()):
        flag = "" if count > 0 else "  <-- ALERTA: 0 chunks, revisar rglob/subcarpeta"
        print(f"   - {domain}: {count} chunk(s){flag}")

    total_expected = sum(counts_by_domain.values())
    store = get_vector_store()
    total_in_store = len(store.get()["ids"])
    match = "OK" if total_in_store == total_expected else "MISMATCH"
    print(f"4. Verificación: chunks generados={total_expected}, en vector store={total_in_store} [{match}]")

    print("\nListo. Siguiente paso: python rag/retrievers.py para probar consultas por dominio.")
