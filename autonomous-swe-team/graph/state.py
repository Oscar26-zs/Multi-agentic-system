"""Definición del estado compartido entre agentes.

Qué hace:
    Declara el TypedDict `EngineeringState`, única fuente de verdad que fluye
    por todos los nodos del grafo de LangGraph.

Responsabilidad dentro del sistema:
    Garantiza que todos los agentes lean/escriban sobre un contrato común:
    requerimiento original, especificación, propuesta técnica, diffs de código,
    hallazgos de seguridad, resultados de pruebas, veredicto del revisor,
    contador de iteraciones, etc.

Se espera que contenga cuando se implemente:
    - TypedDict (o modelo Pydantic) `EngineeringState` con campos tipados y
      documentados.
    - Anotaciones/reducers para los campos que requieran lógica de merge
      entre actualizaciones parciales de nodos.
"""
