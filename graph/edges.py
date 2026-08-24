"""Conditional edges del grafo.

Qué hace:
    Define el enrutamiento entre nodos según el resultado del Reviewer Agent
    (status APPROVED / REJECTED y campo return_to).

Responsabilidad dentro del sistema:
    Implementa el ciclo de revisión: si el veredicto es APPROVED el flujo
    termina (END); si es REJECTED se redirige al agente indicado en return_to;
    si se alcanza MAX_ITERATIONS se corta el ciclo para evitar bucles infinitos.

Se espera que contenga cuando se implemente:
    - Funciones de routing puras que leen el estado compartido y devuelven el
      nombre del siguiente nodo.
    - Mapeos de rutas válidas y protección contra ciclos infinitos.
"""
