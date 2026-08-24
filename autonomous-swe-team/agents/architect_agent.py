"""Agente Arquitecto (architect_agent).

Qué hace:
    Transforma el requerimiento (y la especificación del Product Agent) en una
    propuesta técnica, consultando el RAG de arquitectura.

Responsabilidad dentro del sistema:
    Define el "cómo": stack, componentes, patrones y decisiones técnicas que
    guiarán al Developer Agent. Debe limitarse a lo permitido/recomendado por
    las guías en knowledge/architecture/.

Se espera que contenga cuando se implemente:
    - Prompt de sistema con el rol de arquitecto de software.
    - Integración con el retriever RAG de arquitectura para recuperar
      architecture-guidelines.md y api-design-guidelines.md.
    - Generación de la propuesta técnica con structured output (componentes,
      decisiones, trade-offs y plan de alto nivel).
    - Registro de las fuentes RAG citadas en el estado compartido.
"""
