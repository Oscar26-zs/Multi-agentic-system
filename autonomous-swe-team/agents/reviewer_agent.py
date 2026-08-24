"""Agente Revisor (reviewer_agent).

Qué hace:
    Evalúa el resultado producido por todos los agentes anteriores y decide el
    veredicto final: APPROVED o REJECTED, usando structured output.

Responsabilidad dentro del sistema:
    Punto de decisión del ciclo de revisión. Su salida condiciona las
    conditional edges del grafo: APPROVED termina el flujo; REJECTED puede
    devolver el trabajo a un agente específico (return_to) hasta agotar
    MAX_ITERATIONS.

Se espera que contenga cuando se implemente:
    - Prompt de sistema con el rol de tech lead / revisor crítico.
    - Esquema Pydantic del veredicto (status: APPROVED | REJECTED, feedback,
      return_to, motivos).
    - Evaluación cruzada de especificación, arquitectura, código, seguridad y
      resultados de pruebas.
    - Feedback concreto y accionable dirigido al agente destino.
"""
