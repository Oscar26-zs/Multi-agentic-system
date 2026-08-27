"""Pruebas del flujo completo de LangGraph (Fase 8 de Guia_Construccion.md).

Qué hace:
    Prueba el ENSAMBLAJE del grafo (graph/workflow.py, graph/edges.py) sin
    gastar cuota de ningún proveedor LLM: la lógica de routing condicional
    (camino feliz, ciclo de revisión, límite MAX_ITERATIONS) es independiente
    de qué diga un LLM en particular, así que se prueba con los 6 nodos
    reemplazados por funciones fake que devuelven un update parcial fijo —
    igual forma que devolvería cada agente real (Fase 6), pero instantáneo y
    determinista.

Responsabilidad dentro del sistema:
    Confirma que graph/workflow.py cablea correctamente lo que graph/edges.py
    ya decide (route_after_review, advance_iteration, escalate_to_human) —
    sin este archivo, un error de cableado (ej. un ROUTE_PATH_MAP mal armado)
    solo se vería corriendo el pipeline completo con LLMs reales.

Decisiones (Fase 8 de Guia_Construccion.md):
    - graph/edges.py ya trae su propio smoke test con 4 escenarios validados
      a mano (ver su bloque if __name__ == "__main__"); acá se portan esos
      mismos 4 escenarios a pytest real, para que corran en CI/pre-commit sin
      necesitar que alguien los ejecute manualmente.
    - build_graph() se llama de nuevo en cada test (no se reusa el
      graph_app global del módulo) precisamente porque su docstring lo diseñó
      así: "para que tests/test_graph.py (Fase 8) pueda construir instancias
      frescas si lo necesita, en vez de compartir un único grafo global
      mutable" — necesario acá porque cada test monkeypatchea nodos distintos.
    - Los fakes se inyectan monkeypatcheando graph.workflow.<nombre>_node
      (no agents.*): build_graph() resuelve esos nombres como globals de su
      propio módulo en el momento en que se llama, así que parchear el
      atributo del módulo workflow antes de llamar a build_graph() alcanza,
      sin tocar graph/nodes.py ni los agentes reales.
    - request_plan_approval (graph/edges.py) TAMBIÉN se mockea acá, con un
      fake parametrizable (aprobar_plan, default True): ese nodo hace un
      input() real para el gate de Human-in-the-Loop — sin mockearlo, estos
      tests colgarían la suite esperando teclado. El caso "rechazado" se
      prueba explícito en test_plan_rechazado_por_humano_cancela_el_pipeline.
    - invoke() se llama SIN config (sin callback handler de Langfuse): estos
      tests no hacen ninguna llamada LLM real, así que no hay nada que trazar
      y evitamos una dependencia de red para un test que debe poder correr
      offline.
"""

import pytest

import graph.workflow as workflow_mod
from graph.edges import (
    MAX_ITERATIONS,
    advance_iteration,
    cancelled_by_human,
    escalate_to_human,
    route_after_plan_approval,
    route_after_review,
)
from graph.state import create_initial_state
from langgraph.graph import END

REQUIREMENT = "Como empleado quiero poder solicitar vacaciones."


# ---------- graph/edges.py: los 4 escenarios de su propio smoke test, como pytest real ----------


def test_route_after_review_approved_va_a_end_sin_importar_iteration():
    estado = {"review": {"status": "APPROVED", "return_to": None}, "iteration": 0}
    assert route_after_review(estado) == END
    assert advance_iteration(estado) == {}  # APPROVED no incrementa el contador


def test_route_after_review_rejected_bajo_el_limite_vuelve_al_return_to():
    estado = {"review": {"status": "REJECTED", "return_to": "architect_agent"}, "iteration": 0}
    assert advance_iteration(estado) == {"iteration": 1}
    estado_tras_incremento = {**estado, "iteration": 1}
    assert route_after_review(estado_tras_incremento) == "architect_agent"


def test_route_after_review_rejected_en_el_limite_escala_a_humano():
    estado = {"review": {"status": "REJECTED", "return_to": "developer_agent"}, "iteration": MAX_ITERATIONS}
    assert route_after_review(estado) == "escalate_to_human"
    assert escalate_to_human(estado) == {"human_review_required": True}


def test_route_after_review_rejected_sin_return_to_valido_escala_a_humano():
    """Caso defensivo: no debería ocurrir en la práctica (reviewer_agent._coerce_verdict
    ya garantiza un return_to válido cuando status=REJECTED), pero cubre una
    futura ruptura de ese contrato."""
    estado = {"review": {"status": "REJECTED", "return_to": None}, "iteration": 0}
    assert route_after_review(estado) == "escalate_to_human"


# ---------- graph/edges.py: gate de Human-in-the-Loop (segundo punto de HITL) ----------


def test_route_after_plan_approval_aprobado_va_a_developer():
    estado = {"plan_approval": {"approved": True}}
    assert route_after_plan_approval(estado) == "developer_agent"


def test_route_after_plan_approval_rechazado_va_a_cancelled():
    estado = {"plan_approval": {"approved": False}}
    assert route_after_plan_approval(estado) == "cancelled_by_human"


def test_route_after_plan_approval_sin_campo_va_a_cancelled():
    """Caso defensivo: sin plan_approval en el estado, no se asume aprobado."""
    assert route_after_plan_approval({}) == "cancelled_by_human"


def test_cancelled_by_human_deja_status_cancelled_y_escala():
    resultado = cancelled_by_human({})
    assert resultado["review"]["status"] == "CANCELLED"
    assert resultado["review"]["return_to"] is None
    assert resultado["human_review_required"] is True


# ---------- graph/workflow.py: ensamblaje completo con nodos fake (sin LLM) ----------


def _fake_node(field: str, value: dict, etiqueta: str):
    def _node(state):
        return {field: value, "messages": [etiqueta]}

    return _node


def _make_reviewer_fake(reviews: list[dict]):
    """Devuelve reviews[i] en la i-ésima invocación; repite el último si se
    invoca más veces de las que la lista tiene (útil para el escenario de
    MAX_ITERATIONS, donde el mismo veredicto se repite varias vueltas)."""
    llamadas = {"n": 0}

    def _node(state):
        idx = min(llamadas["n"], len(reviews) - 1)
        llamadas["n"] += 1
        review = reviews[idx]
        return {"review": review, "messages": [f"reviewer_agent:{review['status']}"]}

    _node.llamadas = llamadas
    return _node


def _fake_plan_approval(aprobar: bool):
    def _node(state):
        return {
            "plan_approval": {"approved": aprobar, "note": "fake de test"},
            "messages": [f"request_plan_approval:{'aprobado' if aprobar else 'rechazado'}"],
        }

    return _node


@pytest.fixture()
def patched_graph(monkeypatch):
    """Devuelve build_fn(reviews, aprobar_plan=True) — arma un grafo fresco
    con 5 nodos fake fijos + un reviewer fake parametrizable + el gate de
    Human-in-the-Loop mockeado (sin tocar stdin real). Devuelve
    (grafo, reviewer_fake)."""

    monkeypatch.setattr(workflow_mod, "product_agent_node", _fake_node("specification", {"resumen": "spec"}, "product_agent"))
    monkeypatch.setattr(workflow_mod, "architect_agent_node", _fake_node("architecture", {"resumen": "arch"}, "architect_agent"))
    monkeypatch.setattr(workflow_mod, "developer_agent_node", _fake_node("implementation", {"resumen": "impl"}, "developer_agent"))
    monkeypatch.setattr(workflow_mod, "security_agent_node", _fake_node("security_review", {"aprobado": True}, "security_agent"))
    monkeypatch.setattr(workflow_mod, "testing_agent_node", _fake_node("test_results", {"aprobado": True}, "testing_agent"))

    def _build(reviews: list[dict], aprobar_plan: bool = True):
        reviewer_fake = _make_reviewer_fake(reviews)
        monkeypatch.setattr(workflow_mod, "reviewer_agent_node", reviewer_fake)
        monkeypatch.setattr(workflow_mod, "request_plan_approval", _fake_plan_approval(aprobar_plan))
        return workflow_mod.build_graph(), reviewer_fake

    return _build


def test_camino_feliz_approved_en_la_primera_pasada(patched_graph):
    grafo, reviewer_fake = patched_graph(
        [{"status": "APPROVED", "return_to": None}]
    )
    estado_inicial = create_initial_state(REQUIREMENT)

    resultado = grafo.invoke(estado_inicial)

    assert resultado["review"]["status"] == "APPROVED"
    assert resultado["iteration"] == 0
    assert resultado["human_review_required"] is False
    for campo in ("specification", "architecture", "implementation", "security_review", "test_results"):
        assert resultado[campo], f"{campo} debería haber quedado poblado por el camino feliz"
    assert reviewer_fake.llamadas["n"] == 1


def test_ciclo_de_revision_rejected_vuelve_y_luego_aprueba(patched_graph):
    grafo, reviewer_fake = patched_graph(
        [
            {"status": "REJECTED", "return_to": "architect_agent"},
            {"status": "APPROVED", "return_to": None},
        ]
    )
    estado_inicial = create_initial_state(REQUIREMENT)

    resultado = grafo.invoke(estado_inicial)

    assert resultado["review"]["status"] == "APPROVED"
    assert resultado["iteration"] == 1  # una sola vuelta de rechazo antes de aprobar
    assert resultado["human_review_required"] is False
    assert reviewer_fake.llamadas["n"] == 2
    # el ciclo reingresó por architect_agent -> quedan dos entradas de architect_agent en la bitácora
    assert resultado["messages"].count("architect_agent") == 2


def test_max_iterations_agotado_escala_a_humano(patched_graph):
    grafo, reviewer_fake = patched_graph(
        [{"status": "REJECTED", "return_to": "developer_agent"}]  # se repite cada vuelta
    )
    estado_inicial = create_initial_state(REQUIREMENT)

    resultado = grafo.invoke(estado_inicial)

    assert resultado["human_review_required"] is True
    assert resultado["iteration"] == MAX_ITERATIONS
    assert resultado["review"]["status"] == "REJECTED"
    assert reviewer_fake.llamadas["n"] == MAX_ITERATIONS


def test_build_graph_valida_que_todos_los_destinos_de_route_existan(patched_graph):
    """Si ROUTE_PATH_MAP referenciara un nodo que no existe, build_graph()
    (vía StateGraph.compile()) lanzaría acá, no en tiempo de ejecución."""
    grafo, _ = patched_graph([{"status": "APPROVED", "return_to": None}])
    assert grafo is not None


def test_plan_rechazado_por_humano_cancela_el_pipeline(patched_graph):
    """El gate de Human-in-the-Loop corta ANTES de developer_agent: si el
    usuario rechaza, el pipeline nunca debería llegar a tocar el repo real."""
    grafo, reviewer_fake = patched_graph(
        [{"status": "APPROVED", "return_to": None}], aprobar_plan=False
    )
    estado_inicial = create_initial_state(REQUIREMENT)

    resultado = grafo.invoke(estado_inicial)

    assert resultado["review"]["status"] == "CANCELLED"
    assert resultado["human_review_required"] is True
    assert resultado["plan_approval"]["approved"] is False
    assert not resultado.get("implementation"), "developer_agent no debería haber corrido"
    assert reviewer_fake.llamadas["n"] == 0, "reviewer_agent no debería haber corrido"
