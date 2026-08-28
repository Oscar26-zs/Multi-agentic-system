"""Estado compartido entre agentes (Fase 1 de Guia_Construccion.md).

Qué hace:
    Declara el TypedDict `EngineeringState`: el único "expediente" que fluye
    por todos los nodos del grafo de LangGraph. Cada agente lee lo que
    necesita y agrega su parte; LangGraph se encarga de pasarlo de nodo en
    nodo y de fusionar las actualizaciones parciales que devuelve cada uno.

Responsabilidad dentro del sistema:
    Es EL CONTRATO del sistema multiagente, definido ANTES que cualquier
    agente para evitar retrabajos. Todos los agentes leen/escriben estos
    campos y nada más: requirement -> specification -> architecture ->
    implementation -> security_review -> test_results -> review.

Se espera que contenga cuando se implemente:
    - Los 10 campos base definidos abajo (uno por salida de agente + control
      del ciclo de revisión).
    - Reducers Annotated donde el merge correcto sea ACUMULAR (messages,
      errors) en vez de sobrescribir.
    - Factory create_initial_state() para construir el estado inicial de
      forma consistente desde app.py y los tests.
"""

import operator
from typing import Annotated, TypedDict


class EngineeringState(TypedDict):
    """Contrato de datos que viaja por todo el grafo.

    Convención:
        - Campos "de salida": cada agente ESCRIBE una vez el suyo (dict con
          structured output) y solo LEE los de los agentes anteriores.
        - LangGraph hace merge de lo que cada nodo devuelve; si dos nodos
          tocaran el mismo campo, gana el último en escribir.
    """

    # --- Entrada original del usuario ---
    requirement: str

    # --- Salidas de cada agente (structured output) ---
    specification: dict       # Product Agent: actores, reglas de negocio, criterios de aceptación, riesgos
    architecture: dict        # Architect Agent: stack, componentes, decisiones técnicas, plan
    implementation: dict      # Developer Agent: archivos creados/modificados + diff
    security_review: dict     # Security Agent: hallazgos (severidad, descripción, recomendación)
    test_results: dict        # Testing Agent: casos ejecutados, pass/fail, cobertura

    # --- Veredicto final ---
    review: dict              # Reviewer Agent: status APPROVED|REJECTED|CANCELLED, feedback, return_to

    # --- Human-in-the-Loop: aprobación del plan antes de tocar el repo real ---
    plan_approval: dict       # request_plan_approval (graph/edges.py): {"approved": bool, "note": str}

    # --- Modo propuesta: los agentes Developer/Testing generan una propuesta de
    # cambios (diffs + casos de test) sin escribir/ejecutar nada en el repo real ---
    proposal_mode: bool       # True => Developer/Testing proponen en vez de aplicar/ejecutar

    # --- Control del ciclo de revisión ---
    iteration: int            # contador de vueltas del ciclo; lo compara MAX_ITERATIONS en workflow.py

    # --- Canales que se ACUMULAN entre nodos (no se sobrescriben) ---
    messages: Annotated[list, operator.add]   # bitácora cronológica: qué agente hizo qué
    errors: Annotated[list, operator.add]     # errores capturados por nodo sin tumbar el flujo

    # --- Escalado a humano ---
    human_review_required: bool


def create_initial_state(requirement: str, proposal_mode: bool = False) -> EngineeringState:
    """Construye el estado inicial consistente para cualquier entrypoint.

    Usada por app.py y por tests/test_graph.py / test_scenarios.py
    para no repetir defaults ni cometer typos en las llaves.

    proposal_mode=True pone el pipeline en modo "solo propuesta": los agentes
    Developer y Testing generan diffs/casos de test sin tocar el repo real.
    """
    return {
        "requirement": requirement,
        "specification": {},
        "architecture": {},
        "implementation": {},
        "security_review": {},
        "test_results": {},
        "review": {},
        "plan_approval": {},
        "proposal_mode": proposal_mode,
        "iteration": 0,
        "messages": [],
        "errors": [],
        "human_review_required": False,
    }
