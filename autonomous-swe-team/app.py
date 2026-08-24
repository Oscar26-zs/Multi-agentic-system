"""Punto de entrada de la aplicación.

Qué hace:
    Recibe el requerimiento del usuario y lo inyecta en el workflow compilado
    de LangGraph, mostrando el progreso y el resultado final.

Responsabilidad dentro del sistema:
    Interfaz externa de orquestación; no contiene lógica de agentes. Solo
    parsea input, inicializa configuración/observabilidad e invoca el grafo.

Se espera que contenga cuando se implemente:
    - Elección de interfaz: CLI (argparse), Streamlit o API (FastAPI).
    - Carga de variables de entorno (.env) y chequeo de configuración.
    - Construcción del estado inicial {requerimiento, ...} e invocación del
      grafo compilado (graph/workflow.py).
    - Presentación del resultado final: veredicto, diffs, hallazgos y métricas.
"""
