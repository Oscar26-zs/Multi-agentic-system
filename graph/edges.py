"""Conditional edges del grafo.

Qué hace:
    Define el enrutamiento entre nodos según el resultado del Reviewer Agent
    (status APPROVED / REJECTED y campo return_to).

Responsabilidad dentro del sistema:
    Implementa el ciclo de revisión: si el veredicto es APPROVED el flujo
    termina (END); si es REJECTED se redirige al agente indicado en return_to;
    si se alcanza MAX_ITERATIONS se corta el ciclo para evitar bucles infinitos.

Decisiones (Fase 7 de Guia_Construccion.md, paso 3/3):
    - El incremento de state["iteration"] NO puede vivir dentro de la función
      de routing: las funciones que recibe add_conditional_edges() en
      LangGraph son de solo lectura sobre el estado (reciben state, devuelven
      un string) y no pueden devolver un update. Por eso advance_iteration()
      está definido acá como un NODO real (state -> dict), no como parte de
      route_after_review(); se registra con add_node() en workflow.py,
      insertado entre reviewer_agent y la conditional edge. Solo incrementa
      si el veredicto fue REJECTED — un APPROVED en la primera pasada deja
      iteration en 0 (el contador cuenta vueltas de rechazo, no nodos
      visitados).
    - route_after_review() confía en review["return_to"] como nombre de nodo
      directo: agents/reviewer_agent.py (_coerce_verdict) ya garantiza que
      sea uno de los 5 agentes válidos cuando status=REJECTED, o None cuando
      status=APPROVED. La validación contra _LOOP_TARGETS es defensiva
      (protege contra una futura ruptura de ese contrato), no se espera que
      dispare en la práctica.
    - Al llegar a MAX_ITERATIONS con un REJECTED sin resolver, se enruta a
      escalate_to_human en vez de terminar en silencio: ese nodo deja
      human_review_required=True, el campo que graph/state.py ya reserva
      exactamente para este caso.
    - MAX_ITERATIONS = 3 (fijado por Guia_Construccion.md, no configurable
      por env var en esta fase: es una decisión de diseño del pipeline, no un
      parámetro de despliegue).
    - request_plan_approval / route_after_plan_approval / cancelled_by_human:
      segundo punto de Human-in-the-Loop (el primero es escalate_to_human de
      arriba), agregado a pedido explícito de la consigna del proyecto
      académico (sección 24: "Agent wants to modify > 5 files -> Human
      Approval"). developer_agent es el único agente autorizado a escribir en
      el repo real vía MCP, y lo hacía sin ningún control humano antes de
      actuar — este gate le da al usuario la última palabra antes de esa
      escritura irreversible. Vive en este archivo (no en uno nuevo) porque
      es, igual que advance_iteration/escalate_to_human, una función pura sin
      LLM/RAG/MCP — la única diferencia es que su "trabajo" es leer stdin en
      vez de leer el estado.
    - request_plan_approval() no incrementa iteration ni toca el ciclo de
      revisión: son mecanismos independientes. Un plan rechazado por el
      humano no es un REJECTED del Reviewer (no hay feedback para que un
      agente reintente) — es una cancelación explícita del usuario, con su
      propio status ("CANCELLED") para que no se confunda con un rechazo
      técnico del pipeline.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Permite "python graph/edges.py" como script suelto (Fase 7 de la guía),
    # mismo patrón que usan los 6 agentes de agents/*.py y graph/nodes.py.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import END

from graph.state import EngineeringState

__all__ = [
    "MAX_ITERATIONS",
    "advance_iteration",
    "route_after_review",
    "escalate_to_human",
    "ROUTE_PATH_MAP",
    "request_plan_approval",
    "route_after_plan_approval",
    "cancelled_by_human",
]

MAX_ITERATIONS = 3

_LOOP_TARGETS = {
    "product_agent",
    "architect_agent",
    "developer_agent",
    "security_agent",
    "testing_agent",
}

# path_map explícito para add_conditional_edges(): identidad + los dos
# destinos especiales, para que LangGraph valide en .compile() que todo
# nombre que route_after_review() pueda devolver corresponde a un nodo real.
ROUTE_PATH_MAP = {name: name for name in _LOOP_TARGETS} | {
    "escalate_to_human": "escalate_to_human",
    END: END,
}


def advance_iteration(state: EngineeringState) -> dict:
    """Nodo (no función de routing): incrementa iteration solo si hubo REJECTED.

    Se ejecuta siempre después de reviewer_agent, antes de que
    route_after_review decida el siguiente nodo.
    """
    if state.get("review", {}).get("status") == "REJECTED":
        return {"iteration": state.get("iteration", 0) + 1}
    return {}


def route_after_review(state: EngineeringState) -> str:
    """Decide el siguiente nodo tras reviewer_agent + advance_iteration.

    APPROVED -> END.
    REJECTED con iteration ya en el límite -> escalate_to_human.
    REJECTED con return_to válido -> ese nodo (reingresa la cadena lineal).
    Cualquier otro caso (defensivo, no debería ocurrir) -> escalate_to_human.
    """
    review = state.get("review", {})

    if review.get("status") == "APPROVED":
        return END

    if state.get("iteration", 0) >= MAX_ITERATIONS:
        return "escalate_to_human"

    return_to = review.get("return_to")
    if return_to not in _LOOP_TARGETS:
        return "escalate_to_human"

    return return_to


def escalate_to_human(state: EngineeringState) -> dict:
    """Nodo terminal del ciclo de revisión cuando se agota MAX_ITERATIONS
    sin llegar a APPROVED: deja constancia explícita en el estado en vez de
    terminar sin señal."""
    return {"human_review_required": True}


# ---------- Human-in-the-Loop: aprobación del plan antes de developer_agent ----------


def request_plan_approval(state: EngineeringState) -> dict:
    """Nodo real de pausa: muestra el plan de architect_agent y espera por
    teclado (input() bloqueante) la aprobación humana ANTES de dejar avanzar
    a developer_agent, que es quien de verdad escribe sobre el repo real.

    Se ejecuta siempre después de architect_agent (ver graph/workflow.py).
    """
    architecture = state.get("architecture", {})

    print("\n" + "=" * 70)
    print("HUMAN-IN-THE-LOOP: aprobación requerida antes de tocar el repo real")
    print("=" * 70)
    print(f"Resumen: {architecture.get('resumen', '(sin resumen)')}")
    print("Stack: " + (", ".join(architecture.get("stack", [])) or "(vacío)"))
    print("Componentes:")
    for componente in architecture.get("componentes", []):
        print(f"  - {componente}")
    print("Plan de alto nivel (lo que developer_agent va a ejecutar):")
    for paso in architecture.get("plan_alto_nivel", []):
        print(f"  - {paso}")
    if architecture.get("riesgos_tecnicos"):
        print("Riesgos técnicos señalados por Architect Agent:")
        for riesgo in architecture["riesgos_tecnicos"]:
            print(f"  - {riesgo}")

    respuesta = input(
        "\n¿Autorizas a Developer Agent a ejecutar este plan sobre el repo real? [s/N]: "
    ).strip().lower()
    aprobado = respuesta in ("s", "si", "sí", "y", "yes")

    print(f"{'APROBADO' if aprobado else 'RECHAZADO'} por el usuario.\n")

    return {
        "plan_approval": {
            "approved": aprobado,
            "note": "Aprobación manual vía CLI (request_plan_approval).",
        },
        "messages": [
            f"request_plan_approval: plan {'aprobado' if aprobado else 'rechazado'} por el usuario."
        ],
    }


def route_after_plan_approval(state: EngineeringState) -> str:
    """APPROVED -> developer_agent (sigue la cadena lineal). Cualquier otra
    cosa (rechazado, o campo faltante por defensividad) -> cancelled_by_human."""
    if state.get("plan_approval", {}).get("approved") is True:
        return "developer_agent"
    return "cancelled_by_human"


def cancelled_by_human(state: EngineeringState) -> dict:
    """Nodo terminal cuando el usuario NO autoriza el plan: no es un REJECTED
    del Reviewer (no hay feedback para que un agente reintente), es una
    cancelación explícita — status propio ("CANCELLED") para no confundirla
    con un rechazo técnico del pipeline."""
    return {
        "review": {
            "status": "CANCELLED",
            "resumen": "El usuario no autorizó ejecutar el plan sobre el repositorio real.",
            "motivos": ["Rechazado en el punto de aprobación humana, antes de developer_agent."],
            "feedback": "",
            "return_to": None,
        },
        "human_review_required": True,
    }


if __name__ == "__main__":
    print("Fase 7 (paso 3/3) — smoke test de graph/edges.py")
    print("Funciones puras: sin LLM, sin RAG, sin MCP, sin API key.\n")

    print("1. APPROVED -> debe rutear a END, sin importar iteration...")
    estado = {"review": {"status": "APPROVED", "return_to": None}, "iteration": 0}
    destino = route_after_review(estado)
    assert destino == END, f"esperado END, se obtuvo {destino!r}"
    update = advance_iteration(estado)
    assert update == {}, f"APPROVED no debería incrementar iteration, update={update!r}"
    print(f"   OK - destino={destino!r}, advance_iteration no toca el estado.")

    print("2. REJECTED con return_to='architect_agent', iteration=0 (< MAX) -> vuelve a architect_agent...")
    estado = {"review": {"status": "REJECTED", "return_to": "architect_agent"}, "iteration": 0}
    update = advance_iteration(estado)
    assert update == {"iteration": 1}, f"esperado iteration=1, update={update!r}"
    estado_tras_incremento = {**estado, "iteration": update["iteration"]}
    destino = route_after_review(estado_tras_incremento)
    assert destino == "architect_agent", f"esperado 'architect_agent', se obtuvo {destino!r}"
    print(f"   OK - iteration incrementado a {update['iteration']}, destino={destino!r}.")

    print("3. REJECTED con iteration ya en MAX_ITERATIONS -> escalate_to_human...")
    estado = {"review": {"status": "REJECTED", "return_to": "developer_agent"}, "iteration": MAX_ITERATIONS}
    destino = route_after_review(estado)
    assert destino == "escalate_to_human", f"esperado 'escalate_to_human', se obtuvo {destino!r}"
    resultado_escalada = escalate_to_human(estado)
    assert resultado_escalada == {"human_review_required": True}
    print(f"   OK - destino={destino!r}, escalate_to_human devuelve {resultado_escalada!r}.")

    print("4. REJECTED con return_to inválido/faltante (caso defensivo) -> escalate_to_human...")
    estado = {"review": {"status": "REJECTED", "return_to": None}, "iteration": 0}
    destino = route_after_review(estado)
    assert destino == "escalate_to_human", f"esperado 'escalate_to_human', se obtuvo {destino!r}"
    print(f"   OK - destino={destino!r} (return_to=None con status=REJECTED no debería ocurrir en la práctica; "
          "_coerce_verdict de reviewer_agent.py lo previene, pero esta rama cubre una futura ruptura de ese contrato).")

    print(f"\n5. ROUTE_PATH_MAP tiene {len(ROUTE_PATH_MAP)} entradas: {sorted(str(k) for k in ROUTE_PATH_MAP)}")

    print("\n6. route_after_plan_approval: aprobado -> developer_agent, rechazado -> cancelled_by_human...")
    estado = {"plan_approval": {"approved": True}}
    destino = route_after_plan_approval(estado)
    assert destino == "developer_agent", f"esperado 'developer_agent', se obtuvo {destino!r}"
    estado = {"plan_approval": {"approved": False}}
    destino = route_after_plan_approval(estado)
    assert destino == "cancelled_by_human", f"esperado 'cancelled_by_human', se obtuvo {destino!r}"
    resultado_cancelado = cancelled_by_human(estado)
    assert resultado_cancelado["review"]["status"] == "CANCELLED"
    assert resultado_cancelado["human_review_required"] is True
    print("   OK - ambas ramas de route_after_plan_approval y cancelled_by_human funcionan "
          "(request_plan_approval no se prueba acá porque hace input() real; se prueba en "
          "tests/test_graph.py con stdin mockeado).")

    print("\nListo. edges.py funciona de forma aislada. Siguiente paso: enchufarlo en "
          "graph/workflow.py junto con advance_iteration como nodo real (Fase 7).")
