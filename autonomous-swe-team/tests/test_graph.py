"""Pruebas del flujo completo de LangGraph.

Se espera que contenga cuando se implemente:
    - Test del camino feliz: requerimiento -> ... -> veredicto APPROVED.
    - Test del ciclo de revisión: REJECTED con return_to devuelve el trabajo al
      agente indicado y reintenta correctamente.
    - Test del límite MAX_ITERATIONS: el flujo corta el ciclo sin colgarse.
    - Verificación del contenido del estado compartido en cada etapa.
"""
