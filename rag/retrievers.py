"""Retrievers especializados por dominio.

Qué hace:
    Expone un retriever por área de conocimiento para que cada agente consulte
    únicamente la información relevante a su rol.

Responsabilidad dentro del sistema:
    Interfaz uniforme de recuperación sobre el vector store: dominio
    arquitectura (Architect Agent), seguridad (Security Agent), desarrollo
    (Developer Agent) y testing (Testing Agent).

Se espera que contenga cuando se implemente:
    - Fábrica o funciones constructoras de retrievers filtrando por metadato
      de dominio (architecture/security/development/testing).
    - Configuración de k, score mínimo y eventual reformulación de queries.
"""
