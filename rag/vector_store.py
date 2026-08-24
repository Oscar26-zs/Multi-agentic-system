"""Configuración e inicialización de la base vectorial.

Qué hace:
    Centraliza la creación/conexión al vector store elegido (Chroma) y la
    configuración de su colección única de conocimiento.

Responsabilidad dentro del sistema:
    Único punto de acoplamiento con la tecnología vectorial: ingestion.py y
    retrievers.py dependen de esta capa, lo que facilita cambiar de proveedor
    sin tocar el resto del sistema.

Decisiones (Fase 4 de Guia_Construccion.md):
    - Backend: Chroma, seleccionado vía VECTOR_STORE=chroma en .env. Otro
      valor lanza NotImplementedError explícito (sin fallback silencioso).
    - Persistencia: VECTOR_STORE_PERSIST_DIR (./chroma_db por defecto).
    - Embeddings: modelo ONNX MiniLM-L6-v2 que trae chromadb embebido
      (chromadb.utils.embedding_functions.DefaultEmbeddingFunction), NO
      sentence-transformers/HuggingFace vía torch. Se eligió así por
      restricción real de espacio en disco del entorno de desarrollo (torch +
      transformers pesan varios cientos de MB de instalación); el modelo
      subyacente es el mismo MiniLM-L6-v2 (384 dims) mencionado en
      requirements.txt, solo que exportado a ONNX y ejecutado con
      onnxruntime (dependencia que chromadb ya trae). Como langchain_chroma
      espera un embedding_function con la interfaz langchain_core.Embeddings
      (embed_documents/embed_query), no la interfaz nativa de chromadb
      (__call__), se envuelve con el adaptador ChromaDefaultEmbeddings.
    - Colección única (autonomous-swe-team-knowledge) con metadato "domain"
      por chunk, filtrada en consulta — ver retrievers.py. Se prefirió sobre
      4 colecciones separadas porque Chroma filtra por metadata de forma
      nativa y eficiente, y una colección única no bloquea una futura
      consulta cross-dominio.
"""

import os

from chromadb.utils import embedding_functions
from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings
from dotenv import load_dotenv

load_dotenv()

DEFAULT_COLLECTION_NAME = "autonomous-swe-team-knowledge"

__all__ = [
    "get_embedding_function",
    "get_vector_store",
    "reset_collection",
    "DEFAULT_COLLECTION_NAME",
]


class ChromaDefaultEmbeddings(Embeddings):
    """Adapta el embedding ONNX MiniLM-L6-v2 de chromadb a la interfaz
    langchain_core.embeddings.Embeddings que espera langchain_chroma.Chroma.

    chromadb.utils.embedding_functions.DefaultEmbeddingFunction implementa
    __call__(input: list[str]) -> list[ndarray] (interfaz nativa de chromadb);
    langchain espera embed_documents/embed_query devolviendo list[float].
    """

    def __init__(self) -> None:
        self._chroma_ef = embedding_functions.DefaultEmbeddingFunction()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._chroma_ef(input=texts)
        return [vector.tolist() for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def get_embedding_function() -> Embeddings:
    """Devuelve la función de embeddings local (ONNX MiniLM-L6-v2, sin torch).

    No requiere API key ni conexión externa: onnxruntime ya viene con
    chromadb, y el modelo ONNX se descarga una única vez (~90MB) a un cache
    local la primera vez que se invoca.
    """
    return ChromaDefaultEmbeddings()


def _persist_directory() -> str:
    return os.getenv("VECTOR_STORE_PERSIST_DIR") or "./chroma_db"


def get_vector_store(collection_name: str = DEFAULT_COLLECTION_NAME) -> Chroma:
    """Devuelve el handle persistente a la colección de Chroma, creándola si
    no existe. Lanza NotImplementedError si VECTOR_STORE != "chroma"."""
    backend = (os.getenv("VECTOR_STORE") or "chroma").lower()
    if backend != "chroma":
        raise NotImplementedError(
            f"VECTOR_STORE={backend!r} no está soportado todavía; solo 'chroma'."
        )

    return Chroma(
        collection_name=collection_name,
        embedding_function=get_embedding_function(),
        persist_directory=_persist_directory(),
    )


def reset_collection(collection_name: str = DEFAULT_COLLECTION_NAME) -> None:
    """Borra la colección por completo (usado por ingestion.py antes de
    reindexar, para que reindexar sea idempotente sin duplicar vectores)."""
    store = get_vector_store(collection_name)
    existing_ids = store.get()["ids"]
    if existing_ids:
        store.delete(ids=existing_ids)


if __name__ == "__main__":
    print("Fase 4 — smoke test de rag/vector_store.py")

    print("1. Verificando backend configurado (VECTOR_STORE)...")
    backend = os.getenv("VECTOR_STORE") or "chroma"
    print(f"   OK - backend={backend!r}")

    print("2. Cargando embedding function local (ONNX MiniLM-L6-v2)...")
    embedding_fn = get_embedding_function()
    sample_vector = embedding_fn.embed_query("hola mundo")
    print(f"   OK - vector de prueba generado, dimensión={len(sample_vector)}")

    print("3. Abriendo/creando la colección persistente...")
    store = get_vector_store()
    current_count = len(store.get()["ids"])
    print(f"   OK - persist_directory={_persist_directory()!r}")
    print(f"   OK - colección={DEFAULT_COLLECTION_NAME!r}, documentos actuales={current_count}")

    print(
        "\nListo. La infraestructura del vector store funciona de forma "
        "aislada. Siguiente paso: python rag/ingestion.py para poblarla con "
        "el contenido de knowledge/."
    )
