"""Retrievers especializados por dominio.

Qué hace:
    Expone un retriever por área de conocimiento para que cada agente
    consulte únicamente la información relevante a su rol.

Responsabilidad dentro del sistema:
    Interfaz uniforme de recuperación sobre el vector store (rag/vector_store.py,
    ya poblado por rag/ingestion.py): dominio arquitectura (Architect Agent),
    seguridad (Security Agent), desarrollo (Developer Agent) y testing
    (Testing Agent).

Decisiones (Fase 4 de Guia_Construccion.md):
    - Colección única filtrada por metadato "domain" (ver vector_store.py),
      no 4 colecciones separadas. get_retriever(domain, k) centraliza el
      filtro; las 4 funciones get_<dominio>_retriever() son wrappers finos
      para que cada agente futuro importe algo autodescriptivo en vez de
      pasar strings mágicos, cumpliendo el pedido de la guía de "uno por
      dominio, no un retriever único" sin duplicar la lógica de filtro 4 veces.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.vector_store import get_vector_store

VALID_DOMAINS = {"architecture", "security", "development", "testing"}

__all__ = [
    "get_retriever",
    "get_architecture_retriever",
    "get_security_retriever",
    "get_development_retriever",
    "get_testing_retriever",
]


def get_retriever(domain: str, k: int = 4, score_threshold: float | None = None):
    """Retriever de LangChain filtrado por dominio sobre la colección única.

    Args:
        domain: uno de VALID_DOMAINS (architecture/security/development/testing).
        k: número de fragmentos a devolver.
        score_threshold: si se define, solo devuelve fragmentos con score
            de similitud >= threshold (usa search_type="similarity_score_threshold").
    """
    if domain not in VALID_DOMAINS:
        raise ValueError(f"domain={domain!r} inválido; debe ser uno de {sorted(VALID_DOMAINS)}")

    search_kwargs: dict = {"k": k, "filter": {"domain": domain}}
    search_type = "similarity"
    if score_threshold is not None:
        search_type = "similarity_score_threshold"
        search_kwargs["score_threshold"] = score_threshold

    store = get_vector_store()
    return store.as_retriever(search_type=search_type, search_kwargs=search_kwargs)


def get_architecture_retriever(k: int = 4):
    return get_retriever("architecture", k=k)


def get_security_retriever(k: int = 4):
    return get_retriever("security", k=k)


def get_development_retriever(k: int = 4):
    return get_retriever("development", k=k)


def get_testing_retriever(k: int = 4):
    return get_retriever("testing", k=k)


if __name__ == "__main__":
    print("Fase 4 — smoke test de rag/retrievers.py")

    query = "¿qué pasa si el aprobador es el mismo empleado que creó la solicitud de vacaciones?"
    print(f"1. Consultando el retriever de seguridad con: {query!r}")
    security_retriever = get_security_retriever()
    security_docs = security_retriever.invoke(query)

    if not security_docs:
        raise SystemExit(
            "   ERROR - 0 resultados. Corre primero 'python rag/ingestion.py' "
            "para poblar el vector store."
        )

    for i, doc in enumerate(security_docs, start=1):
        preview = doc.page_content[:200].replace("\n", " ")
        print(f"   [{i}] domain={doc.metadata.get('domain')!r} "
              f"source={doc.metadata.get('source')!r} header={doc.metadata.get('header')!r}")
        print(f"       {preview}...")

    hit = any(doc.metadata.get("source") == "security-guidelines.md" for doc in security_docs)
    print(f"\n   {'OK' if hit else 'ALERTA'} - "
          f"{'se recuperó security-guidelines.md como se esperaba.' if hit else 'no apareció security-guidelines.md; revisar chunking/query.'}")

    print("\n2. Confirmando que el filtro de dominio realmente aísla (probando 'testing')...")
    testing_docs = get_testing_retriever().invoke(query)
    cross_domain_leak = any(doc.metadata.get("domain") != "testing" for doc in testing_docs)
    print(f"   {'ALERTA - hay fuga cross-dominio' if cross_domain_leak else 'OK - todos los resultados son domain=testing'}")

    print(
        "\nListo. rag/ funciona de forma aislada, sin ningún agente todavía. "
        "Siguiente paso de Guia_Construccion.md: Fase 5 (mcp/server.py)."
    )
