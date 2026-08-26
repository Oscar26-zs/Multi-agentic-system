"""Construcción y compilación del grafo completo de LangGraph.

Qué hace:
    Ensambla nodos y edges en un StateGraph y lo compila en el grafo
    ejecutable final.

Responsabilidad dentro del sistema:
    Punto único donde se define la topología del sistema multiagente:
    product -> architect -> developer -> security -> testing -> reviewer,
    incluyendo el ciclo de revisión y el límite MAX_ITERATIONS.

Decisiones (Fase 7 de Guia_Construccion.md, paso 2/3 y 4/3):
    - Cadena lineal Product -> Architect -> Developer -> Security -> Testing
      -> Reviewer (orden fijado por Guia_Construccion.md y por los propios
      agentes: developer_agent.py documenta explícitamente que corre ANTES
      que security_agent y por eso no depende de security_review).
    - advance_iteration (graph/edges.py) se registra como NODO real entre
      reviewer_agent y la conditional edge, no como parte de la función de
      routing: add_conditional_edges() en LangGraph solo permite que la
      función de routing LEA el estado y devuelva un string, no que lo
      mute — el incremento de iteration necesita un nodo de verdad.
    - route_after_review (graph/edges.py) se cablea con add_conditional_edges
      usando ROUTE_PATH_MAP explícito: nombres de nodo idénticos a los
      nombres de función de cada agente, así review["return_to"] sirve
      directamente como destino sin tabla de traducción, y LangGraph valida
      en compile() que todo destino posible exista como nodo real.
    - escalate_to_human (graph/edges.py) tiene una edge fija a END: dejar
      human_review_required=True es la señal, no hace falta más lógica ahí.
    - build_graph() se expone como factory separada de graph_app (el grafo ya
      compilado) para que tests/test_graph.py (Fase 8) pueda construir
      instancias frescas si lo necesita, en vez de compartir un único grafo
      global mutable.
    - Se exporta como "graph_app" (no "graph", para no leer como
      "graph.graph" al importarlo desde el paquete graph/; tampoco "workflow"
      a secas, para dejarle ese nombre a este módulo).
    - No se fija recursion_limit manualmente: se verificó que la versión de
      langgraph instalada en este proyecto (1.2.11) trae
      DEFAULT_RECURSION_LIMIT=10007 (langgraph/_internal/_config.py), muy por
      encima del peor caso real de este grafo (~22 pasos con MAX_ITERATIONS=3
      y el ciclo más largo, product_agent). Fijarlo a mano sería
      infraestructura especulativa para un riesgo que no existe en la
      práctica con esta versión.
    - default_invoke_config() expone get_callback_handler() (ya existe en
      observability/langfuse_config.py desde la Fase 2, nunca usado hasta
      ahora) para quien invoque el grafo: LangGraph propaga
      config["callbacks"] automáticamente a las llamadas LLM internas de
      cada agente sin tocar agents/ ni nodes.py. Cada agente ya está trazado
      individualmente vía @observe; esto además correlaciona sus llamadas
      LLM bajo la misma corrida en Langfuse.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Permite "python graph/workflow.py" como script suelto (Fase 7 de la
    # guía), mismo patrón que agents/*.py, graph/nodes.py y graph/edges.py.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph.edges import ROUTE_PATH_MAP, MAX_ITERATIONS, advance_iteration, escalate_to_human, route_after_review
from graph.nodes import (
    architect_agent_node,
    developer_agent_node,
    product_agent_node,
    reviewer_agent_node,
    security_agent_node,
    testing_agent_node,
)
from graph.state import EngineeringState
from observability.langfuse_config import get_callback_handler

__all__ = ["build_graph", "graph_app", "default_invoke_config", "MAX_ITERATIONS"]


def build_graph() -> CompiledStateGraph:
    """Ensambla y compila el grafo completo: camino lineal + ciclo de revisión."""
    g = StateGraph(EngineeringState)

    g.add_node("product_agent", product_agent_node)
    g.add_node("architect_agent", architect_agent_node)
    g.add_node("developer_agent", developer_agent_node)
    g.add_node("security_agent", security_agent_node)
    g.add_node("testing_agent", testing_agent_node)
    g.add_node("reviewer_agent", reviewer_agent_node)
    g.add_node("advance_iteration", advance_iteration)
    g.add_node("escalate_to_human", escalate_to_human)

    # Camino feliz: Product -> Architect -> Developer -> Security -> Testing -> Reviewer
    g.add_edge(START, "product_agent")
    g.add_edge("product_agent", "architect_agent")
    g.add_edge("architect_agent", "developer_agent")
    g.add_edge("developer_agent", "security_agent")
    g.add_edge("security_agent", "testing_agent")
    g.add_edge("testing_agent", "reviewer_agent")
    g.add_edge("reviewer_agent", "advance_iteration")

    # Ciclo de revisión: APPROVED->END, REJECTED->return_to (reingresa la
    # cadena lineal de arriba), MAX_ITERATIONS agotado -> escalate_to_human.
    g.add_conditional_edges("advance_iteration", route_after_review, ROUTE_PATH_MAP)
    g.add_edge("escalate_to_human", END)

    return g.compile()


def default_invoke_config() -> dict:
    """Config lista para pasar a graph_app.invoke(state, config=...):
    correlaciona en Langfuse las llamadas LLM de todos los nodos de una
    misma corrida, vía el callback handler que observability/langfuse_config
    expone desde la Fase 2 pero que ningún módulo usaba todavía."""
    return {"callbacks": [get_callback_handler()]}


graph_app = build_graph()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    import os

    from graph.state import create_initial_state
    from observability.langfuse_config import flush_traces

    print("Fase 7 (paso 2 y 4/3) — smoke test end-to-end de graph/workflow.py")

    print("1. Verificando OPENROUTER_API_KEY en el entorno...")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit(
            "   ERROR - OPENROUTER_API_KEY no configurada. Completa tu .env "
            "(copia .env.example) antes de correr esta prueba en vivo."
        )
    print("   OK - variable presente.")

    print("2. Corriendo un requerimiento real de punta a punta por los 6 agentes reales...")
    print("   (LLM + RAG + MCP reales, sin mocks — puede tardar varios minutos).")
    estado_inicial = create_initial_state(
        "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
        "y fin, y que mi jefe directo apruebe o rechace la solicitud."
    )
    resultado = graph_app.invoke(estado_inicial, config=default_invoke_config())

    print("3. Verificando que cada etapa del pipeline dejó su parte del estado poblada...")
    campos_esperados = [
        "specification",
        "architecture",
        "implementation",
        "security_review",
        "test_results",
        "review",
    ]
    for campo in campos_esperados:
        valor = resultado.get(campo)
        flag = "OK" if valor else "ALERTA - vacío"
        print(f"   {flag} - {campo}")

    print(f"\n4. Veredicto final: {resultado['review'].get('status')!r} "
          f"(iteration={resultado.get('iteration')}, "
          f"human_review_required={resultado.get('human_review_required')})")

    print("\n5. Bitácora completa (state['messages']):")
    for linea in resultado.get("messages", []):
        print(f"   - {linea}")

    print("\n6. Forzando flush de traces a Langfuse...")
    flush_traces()

    print(
        "\nListo. El grafo corre de punta a punta, camino feliz + ciclo de revisión "
        "cableados. Entra a cloud.langfuse.com -> Traces para ver la corrida completa "
        "con los 6 agentes correlacionados. Siguiente paso: Fase 8 de "
        "Guia_Construccion.md (tests/)."
    )
