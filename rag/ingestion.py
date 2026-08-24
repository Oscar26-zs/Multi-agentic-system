"""Ingesta de documentos hacia el vector store.

Qué hace:
    Carga los documentos markdown de knowledge/, los divide en chunks y genera
    embeddings para almacenarlos en la base vectorial.

Responsabilidad dentro del sistema:
    Prepara la base de conocimiento interna (arquitectura, seguridad,
    desarrollo, testing) para que los retrievers especializados puedan
    consultarla durante la ejecución del pipeline.

Se espera que contenga cuando se implemente:
    - Loaders de documentos (markdown/directory loader).
    - Estrategia de chunking configurable (tamaño, overlap, metadatos de dominio).
    - Generación de embeddings con el modelo elegido y escritura al vector store.
    - Script re-ejecutable para reindexar cuando cambie el contenido de knowledge/.
"""
