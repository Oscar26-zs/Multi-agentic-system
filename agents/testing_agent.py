"""Agente de Testing (testing_agent).

Qué hace:
    Genera y ejecuta la estrategia de pruebas del requerimiento sobre el
    repositorio objetivo, vía la tool MCP run_tests.

Responsabilidad dentro del sistema:
    Verifica objetivamente que la implementación cumple los criterios de
    aceptación definidos por el Product Agent y reporta resultados al resto
    del pipeline.

Se espera que contenga cuando se implemente:
    - Prompt de sistema con el rol de QA engineer.
    - Generación de casos de prueba (unitarios, integración, edge cases)
      basados en los criterios de aceptación y la estrategia de knowledge/testing/.
    - Creación/ejecución de tests usando las tools MCP del servidor propio.
    - Reporte de resultados (pasados/fallidos, cobertura) en el estado
      compartido.
"""
