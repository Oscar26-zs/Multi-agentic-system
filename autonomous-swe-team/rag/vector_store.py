"""Configuración e inicialización de la base vectorial.

Qué hace:
    Centraliza la creación/conexión al vector store elegido (Chroma, FAISS u
    otro proveedor) y la configuración de sus colecciones.

Responsabilidad dentro del sistema:
    Único punto de acoplamiento con la tecnología vectorial: ingestion.py y
    retrievers.py dependen de esta capa, lo que facilita cambiar de proveedor
    sin tocar el resto del sistema.

Se espera que contenga cuando se implemente:
    - Selección del backend por variable de entorno (p. ej. VECTOR_STORE=chroma|faiss).
    - Creación/persistencia de colecciones y rutinas de limpieza/reindexado.
    - Cliente compartido para embeddings y búsqueda por similitud.
"""
