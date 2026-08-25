"""Nodos del grafo de LangGraph.

Qué hace:
    Envuelve a cada agente en una función nodo con la firma estándar de
    LangGraph (estado -> actualizaciones parciales del estado).

Responsabilidad dentro del sistema:
    Desacopla a los agentes del motor de orquestación: cada nodo delega en su
    agente correspondiente y persiste la salida en el estado compartido.

Decisiones (Fase 7 de Guia_Construccion.md, paso 1/3):
    - Wrapper delgado, no reimplementación: cada agente (agents/*.py, Fase 6)
      ya tiene exactamente la firma que un nodo de LangGraph necesita
      (state -> dict de update parcial) y ya está decorado con @observe. Lo
      único que agrega este módulo es capturar la excepción del agente y
      re-lanzarla envuelta en NodeExecutionError, con el nombre del nodo que
      falló, para que el diagnóstico no dependa de leer un traceback genérico
      de Pydantic/LangChain.
    - No se traga la excepción ni se deja continuar el grafo: los 6 agentes
      ya hacen "raise ValueError" ruidoso ante una precondición rota (ej.
      specification vacía). Tragar ese error acá y seguir dejaría al
      siguiente nodo trabajando sobre estado corrupto/incompleto — lo
      contrario del patrón "nunca fallar en silencio" ya establecido en toda
      la Fase 6.
    - Nombres de nodo idénticos al nombre de la función del agente
      ("product_agent", "architect_agent", etc.): así review["return_to"]
      (agents/reviewer_agent.py) puede usarse directamente como nombre de
      nodo destino en la conditional edge de edges.py, sin tabla de
      traducción.
"""

import sys
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):
    # Permite "python graph/nodes.py" como script suelto (Fase 7 de la guía),
    # mismo patrón que usan los 6 agentes de agents/*.py.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.architect_agent import architect_agent
from agents.developer_agent import developer_agent
from agents.product_agent import product_agent
from agents.reviewer_agent import reviewer_agent
from agents.security_agent import security_agent
from agents.testing_agent import testing_agent
from graph.state import EngineeringState

__all__ = [
    "NodeExecutionError",
    "product_agent_node",
    "architect_agent_node",
    "developer_agent_node",
    "security_agent_node",
    "testing_agent_node",
    "reviewer_agent_node",
]


class NodeExecutionError(RuntimeError):
    """Envuelve una excepción de agente con el nombre del nodo que falló.

    original queda accesible para quien quiera inspeccionar la causa real
    (ej. distinguir un ValueError de precondición de un error de red del LLM)
    sin tener que parsear el mensaje.
    """

    def __init__(self, node_name: str, original: Exception) -> None:
        super().__init__(f"[{node_name}] {original}")
        self.node_name = node_name
        self.original = original


def _wrap(agent_fn: Callable[[EngineeringState], dict], node_name: str) -> Callable[[EngineeringState], dict]:
    """Envuelve agent_fn como nodo: misma firma, excepciones etiquetadas con node_name."""

    def _node(state: EngineeringState) -> dict:
        try:
            return agent_fn(state)
        except Exception as exc:
            raise NodeExecutionError(node_name, exc) from exc

    _node.__name__ = node_name
    return _node


product_agent_node = _wrap(product_agent, "product_agent")
architect_agent_node = _wrap(architect_agent, "architect_agent")
developer_agent_node = _wrap(developer_agent, "developer_agent")
security_agent_node = _wrap(security_agent, "security_agent")
testing_agent_node = _wrap(testing_agent, "testing_agent")
reviewer_agent_node = _wrap(reviewer_agent, "reviewer_agent")


if __name__ == "__main__":
    from graph.state import create_initial_state

    print("Fase 7 (paso 1/3) — smoke test de graph/nodes.py")

    print("1. Probando el camino de error SIN llamar a ningún LLM (precondición rota)...")
    estado_incompleto = create_initial_state("requerimiento de prueba")
    # specification queda vacío a propósito: architect_agent debe hacer
    # "raise ValueError" antes de tocar el LLM o el RAG.
    try:
        architect_agent_node(estado_incompleto)
        raise SystemExit("   ERROR - se esperaba NodeExecutionError y no se lanzó ninguna excepción.")
    except NodeExecutionError as exc:
        assert exc.node_name == "architect_agent", f"node_name inesperado: {exc.node_name!r}"
        assert isinstance(exc.original, ValueError), f"tipo de excepción original inesperado: {type(exc.original)}"
        print(f"   OK - NodeExecutionError capturó el fallo de 'architect_agent': {exc}")

    print("2. Verificando OPENROUTER_API_KEY en el entorno (para el resto del smoke test)...")
    import os

    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit(
            "   ERROR - OPENROUTER_API_KEY no configurada. Completa tu .env "
            "(copia .env.example) antes de correr esta prueba en vivo."
        )
    print("   OK - variable presente.")

    print("3. Probando product_agent_node con una llamada real (confirma que el passthrough funciona)...")
    estado_inicial = create_initial_state(
        "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
        "y fin, y que mi jefe directo apruebe o rechace la solicitud."
    )
    resultado = product_agent_node(estado_inicial)
    assert "specification" in resultado and resultado["specification"], (
        "product_agent_node no devolvió una specification no vacía."
    )
    print(f"   OK - product_agent_node devolvió specification con {len(resultado['specification'])} campo(s).")

    from observability.langfuse_config import flush_traces

    print("4. Forzando flush de traces a Langfuse...")
    flush_traces()

    print("\nListo. Los 6 nodos están definidos y el manejo de excepciones funciona. "
          "Siguiente paso: graph/workflow.py (Fase 7, paso 2/3, camino feliz sin ciclos).")
