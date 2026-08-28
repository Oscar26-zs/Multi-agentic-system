"""Agente Revisor (reviewer_agent).

Qué hace:
    Lee TODO el estado acumulado por los cinco agentes anteriores
    (specification, architecture, implementation, security_review,
    test_results) y emite el veredicto final: APPROVED o REJECTED, con
    feedback concreto dirigido a un agente específico (`return_to`) cuando
    rechaza.

Responsabilidad dentro del sistema:
    Punto de decisión del ciclo de revisión. Su salida condiciona las
    conditional edges del grafo (Fase 7): APPROVED termina el flujo;
    REJECTED devuelve el trabajo al agente indicado en `return_to`, hasta
    agotar `MAX_ITERATIONS` (workflow.py). Sexto y último eslabón del
    pipeline (Product -> Architect -> Developer -> Security -> Testing ->
    Reviewer, ver README.md).

Decisiones (Fase 6 de Guia_Construccion.md, agente 6/6):
    - Sin RAG, sin MCP: es el único agente que no introduce ninguna
      dependencia nueva — solo lee el resto del estado y razona sobre él. No
      hay un "manual de estilo" externo que consultar en esta etapa: el
      material de revisión ES el estado que ya dejaron los cinco agentes
      anteriores.
    - `status`/`return_to` NO se dejan enteramente al criterio del LLM: dos
      señales ya calculadas de forma determinista en agentes previos —
      `security_review["aprobado"]` (security_agent.py) y
      `test_results["aprobado"]` (testing_agent.py) — se usan como
      guardrails posteriores a la llamada al LLM (`_coerce_verdict`). Si
      cualquiera de las dos es `False`, el veredicto se fuerza a `REJECTED`
      con un `return_to` fijo, sin importar qué haya decidido el LLM. Mismo
      principio que "no confiar en la memoria del modelo para hechos
      verificables" (ya aplicado en `fuentes_consultadas`, `aprobado` de
      Security, `archivos_creados` de Developer, `passed`/`failed` de
      Testing), llevado a la decisión de más alto riesgo del pipeline: un
      Reviewer que aprueba a pesar de un hallazgo crítico de seguridad o de
      tests en rojo sería el peor lugar posible para que un modelo gratuito
      "se equivoque por ser amable".
    - El guardrail de seguridad manda a `architect_agent` (no a
      `security_agent`): tal como está construido `security_agent.py`
      (agente 3/6), revisa la ARQUITECTURA propuesta, no código — si hay un
      hallazgo bloqueante, lo que hay que corregir es el diseño, no la
      revisión en sí misma.
    - El guardrail de testing manda a `developer_agent`: tests en rojo
      normalmente significan que la implementación no cumple lo que el plan
      decía, no que falten más tests.
    - Fuera de esos dos guardrails, `return_to` queda a criterio del LLM
      entre los cinco agentes válidos (`product_agent` si la ambigüedad
      viene de la especificación, `architect_agent` si el diseño tiene un
      problema no cubierto por el guardrail de seguridad, `developer_agent`
      si el código no sigue el plan, `testing_agent` si faltó cobertura
      aunque los tests que sí corrieron hayan pasado).
    - `reviewer_agent()` NO incrementa `state["iteration"]`: ese contador lo
      administra la conditional edge en `graph/edges.py` (Fase 7, todavía no
      construida) al procesar un `REJECTED`, no este agente — coherente con
      el comentario de `graph/state.py` ("iteration: ... lo compara
      MAX_ITERATIONS en workflow.py").
    - Cliente LLM vía agents/llm_factory.py (build_llm), igual que los cinco
      agentes anteriores.
"""

import json
import os
import sys
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    # Permite "python agents/reviewer_agent.py" como script suelto (Fase 6
    # de la guía): sin esto, sys.path solo contendría agents/, no la raíz del
    # repo, y "from graph.state import ..." fallaría con ModuleNotFoundError.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agents.llm_factory import invoke_structured
from graph.state import EngineeringState, create_initial_state
from observability.langfuse_config import flush_traces, observe

load_dotenv()

__all__ = ["ReviewVerdict", "reviewer_agent"]

_RETURN_TO_AGENTS = ("product_agent", "architect_agent", "developer_agent", "security_agent", "testing_agent")
_DIFF_CHAR_LIMIT = 3000

_SYSTEM_PROMPT = """\
Eres el tech lead de un estudio de ingeniería, y el último filtro antes de
que un cambio se dé por terminado. Recibís el expediente completo de un
requerimiento: la especificación funcional, la propuesta de arquitectura, lo
que implementó el desarrollador, la revisión de seguridad y los resultados
de testing. Tu trabajo es evaluarlo de punta a punta y decidir: ¿esto queda
aprobado (APPROVED), o hay que devolverlo a alguien para que lo corrija
(REJECTED)?

Instrucciones:
- Evaluá el expediente completo, no un solo aspecto: ¿la implementación
  cumple los criterios de aceptación de la especificación? ¿siguió el plan
  de la arquitectura? ¿hay hallazgos de seguridad sin resolver? ¿los tests
  que corrieron reflejan cobertura real de los criterios, no solo que "algo"
  pasó?
- Sé exigente pero justo: un REJECTED sin un motivo concreto y accionable no
  ayuda a nadie. Cada motivo debe ser algo que el agente destino pueda
  corregir, no una impresión vaga.
- Si rechazás, elegí return_to entre exactamente estos valores:
  product_agent (la ambigüedad viene de la especificación original),
  architect_agent (el diseño tiene un problema, incluyendo hallazgos de
  seguridad sin resolver), developer_agent (el código no sigue el plan o
  tiene un bug), testing_agent (falta cobertura de un criterio de
  aceptación, aunque los tests existentes hayan pasado).
- El feedback debe estar dirigido específicamente a ese agente destino, en
  términos que pueda ejecutar en su próxima corrida — no a "el equipo" en
  general.
- Si apruebas, dejá feedback breve confirmando qué quedó verificado, no una
  lista de instrucciones.
- Responde siempre en español.
"""


class ReviewVerdict(BaseModel):
    """Veredicto que produce el LLM. `status`/`return_to` pueden ser
    sobreescritos después por los guardrails deterministas de seguridad y
    testing — ver decisiones en el docstring del módulo."""

    status: Literal["APPROVED", "REJECTED"] = Field(..., description="Veredicto final.")
    resumen: str = Field(..., description="Evaluación breve (1-3 frases) del expediente completo.")
    motivos: list[str] = Field(
        ..., description="Razones concretas detrás del veredicto, una por ítem evaluado relevante."
    )
    feedback: str = Field(
        ...,
        description="Si REJECTED: feedback accionable dirigido al agente en return_to. Si APPROVED: confirmación breve de qué quedó verificado.",
    )
    return_to: Literal["product_agent", "architect_agent", "developer_agent", "security_agent", "testing_agent"] | None = Field(
        default=None,
        description="Obligatorio si status=REJECTED (uno de los 5 agentes); None si status=APPROVED.",
    )


def _truncate(value, limit: int = _DIFF_CHAR_LIMIT):
    """Recorta campos potencialmente largos (ej. implementation['diff']) antes de mandarlos al LLM."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\n... [recortado, {len(value) - limit} caracteres más]"
    return value


def _coerce_verdict(
    verdict: ReviewVerdict,
    security_review: dict,
    test_results: dict,
    proposal_mode: bool = False,
) -> dict:
    """Aplica los guardrails deterministas sobre el veredicto del LLM.

    Nunca deja pasar un APPROVED si security_review["aprobado"] o
    test_results["aprobado"] son explícitamente False — ver decisiones en el
    docstring del módulo.

    En proposal_mode el guardrail de testing se omite: no hay ejecución real de
    tests, así que test_results["aprobado"] es None y no debe forzar REJECTED.
    El guardrail de seguridad sí aplica (revisa la arquitectura propuesta, no
    requiere ejecutar nada).
    """
    data = verdict.model_dump()

    security_bloquea = security_review.get("aprobado") is False
    testing_bloquea = (not proposal_mode) and test_results.get("aprobado") is False

    if security_bloquea or testing_bloquea:
        data["status"] = "REJECTED"
        if security_bloquea:
            data["return_to"] = "architect_agent"
            motivo = "Guardrail determinista: security_review['aprobado'] es False (hay hallazgos de severidad crítica o alta sin resolver)."
        else:
            data["return_to"] = "developer_agent"
            motivo = "Guardrail determinista: test_results['aprobado'] es False (hay tests en rojo, o no se corrió ninguno)."
        if motivo not in data["motivos"]:
            data["motivos"].append(motivo)
    elif data["status"] == "APPROVED":
        data["return_to"] = None
    elif data["status"] == "REJECTED" and data.get("return_to") not in _RETURN_TO_AGENTS:
        data["return_to"] = "developer_agent"
        data["motivos"].append(
            "return_to no especificado (o inválido) por el LLM; se usó developer_agent como destino por defecto."
        )

    return data


@observe(name="reviewer_agent")
def reviewer_agent(state: EngineeringState) -> dict:
    """Evalúa el estado completo y emite el veredicto final.

    Devuelve un update parcial del estado: {"review": {...}, "messages": [...]}.
    No toca state["iteration"] — ver decisiones en el docstring del módulo.
    """
    specification = state.get("specification", {})
    architecture = state.get("architecture", {})
    implementation = state.get("implementation", {})
    security_review = state.get("security_review", {})
    test_results = state.get("test_results", {})

    if not implementation:
        raise ValueError(
            "state['implementation'] está vacío; reviewer_agent necesita el pipeline completo "
            "(al menos hasta developer_agent) antes de emitir un veredicto."
        )

    implementation_para_prompt = dict(implementation)
    if "diff" in implementation_para_prompt:
        implementation_para_prompt["diff"] = _truncate(implementation_para_prompt["diff"])

    mensaje_usuario = (
        f"Iteración actual del ciclo de revisión: {state.get('iteration', 0)}\n\n"
        "Especificación funcional (JSON):\n"
        f"{json.dumps(specification, ensure_ascii=False, indent=2)}\n\n"
        "Propuesta de arquitectura (JSON):\n"
        f"{json.dumps(architecture, ensure_ascii=False, indent=2)}\n\n"
        "Implementación del Developer Agent (JSON, diff recortado si es largo):\n"
        f"{json.dumps(implementation_para_prompt, ensure_ascii=False, indent=2)}\n\n"
        "Revisión de seguridad (JSON):\n"
        f"{json.dumps(security_review, ensure_ascii=False, indent=2)}\n\n"
        "Resultados de testing (JSON):\n"
        f"{json.dumps(test_results, ensure_ascii=False, indent=2)}"
    )

    verdict = invoke_structured(
        ReviewVerdict,
        [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=mensaje_usuario),
        ],
    )

    review = _coerce_verdict(verdict, security_review, test_results, proposal_mode=bool(state.get("proposal_mode", False)))

    return {
        "review": review,
        "messages": [
            f"reviewer_agent: veredicto {review['status']}"
            + (f" (return_to={review['return_to']})" if review["return_to"] else "")
            + f", {len(review['motivos'])} motivo(s)."
        ],
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("Fase 6 (agente 6/6) — smoke test de agents/reviewer_agent.py")

    print("1. Verificando OPENROUTER_API_KEY en el entorno...")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit(
            "   ERROR - OPENROUTER_API_KEY no configurada. Completa tu .env "
            "(copia .env.example) antes de correr esta prueba en vivo."
        )
    print("   OK - variable presente.")

    print("2. Armando un estado completo de prueba (simulando a los cinco agentes anteriores)...")
    requerimiento_ejemplo = sys.argv[1] if len(sys.argv) > 1 else (
        "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
        "y fin, y que mi jefe directo apruebe o rechace la solicitud."
    )
    state = create_initial_state(requerimiento_ejemplo)
    if len(sys.argv) > 1:
        print(
            "   (nota: requirement personalizado; el expediente de prueba de abajo "
            "sigue siendo el del ejemplo de vacaciones, no se deriva de tu texto)"
        )
    state["specification"] = {
        "resumen": "Flujo de solicitud y aprobación de vacaciones entre empleado y aprobador.",
        "actores": ["Empleado", "Aprobador"],
        "reglas_negocio": ["Un aprobador no puede aprobar su propia solicitud."],
        "criterios_aceptacion": ["El empleado puede crear una solicitud con fecha de inicio y fin."],
        "riesgos": ["Auto-aprobación si el aprobador y el autor son el mismo usuario."],
        "supuestos": [],
    }
    state["architecture"] = {
        "resumen": "API REST en ASP.NET Core con validación de negocio en el dominio.",
        "stack": ["ASP.NET Core", "EF Core"],
        "componentes": ["Endpoint de creación", "Endpoint de aprobación"],
        "decisiones_tecnicas": [],
        "plan_alto_nivel": ["Implementar entidad SolicitudVacaciones", "Implementar endpoints"],
        "riesgos_tecnicos": [],
        "fuentes_consultadas": ["architecture-guidelines.md"],
    }
    state["implementation"] = {
        "resumen": "Se implementaron los endpoints de creación y aprobación siguiendo el plan.",
        "notas": [],
        "archivos_creados": ["Web/Controllers/SolicitudVacacionesController.cs"],
        "archivos_modificados": [],
        "diff": "(diff simulado para el smoke test)",
        "pasos_seguidos": [],
        "fuentes_consultadas": ["coding-standards.md"],
    }

    print(
        "3. Escenario A: security_review y test_results 'aprobados' -> el LLM decide libremente..."
    )
    state["security_review"] = {
        "resumen": "Sin hallazgos bloqueantes.",
        "hallazgos": [],
        "riesgos_aceptados": [],
        "fuentes_consultadas": ["security-guidelines.md"],
        "aprobado": True,
    }
    state["test_results"] = {
        "resumen": "Todos los tests pasaron.",
        "casos_generados": [],
        "hallazgos": [],
        "notas": [],
        "passed": 5,
        "failed": 0,
        "skipped": 0,
        "total": 5,
        "aprobado": True,
        "comandos_ejecutados": ["dotnet test"],
        "archivos_creados": [],
        "archivos_modificados": [],
        "pasos_seguidos": [],
        "fuentes_consultadas": ["testing-strategy.md"],
    }

    resultado_a = reviewer_agent(state)
    print("   Veredicto (escenario A):")
    print(json.dumps(resultado_a["review"], indent=2, ensure_ascii=False))

    print(
        "\n4. Escenario B: security_review['aprobado']=False -> el guardrail determinista "
        "debe forzar REJECTED/architect_agent sin importar qué diga el LLM..."
    )
    state["security_review"]["aprobado"] = False
    state["security_review"]["hallazgos"] = [
        {
            "severidad": "alta",
            "categoria_owasp": "A01:2021 - Control de Acceso Roto",
            "descripcion": "Hallazgo simulado para el smoke test.",
            "recomendacion": "Corregir la validación de propiedad del recurso.",
        }
    ]

    resultado_b = reviewer_agent(state)
    print("   Veredicto (escenario B):")
    print(json.dumps(resultado_b["review"], indent=2, ensure_ascii=False))

    assert resultado_b["review"]["status"] == "REJECTED", "El guardrail de seguridad no forzó REJECTED."
    assert resultado_b["review"]["return_to"] == "architect_agent", "El guardrail de seguridad no forzó return_to=architect_agent."
    print("   OK - el guardrail determinista de seguridad se aplicó correctamente.")

    print("5. Forzando flush de traces a Langfuse...")
    flush_traces()

    print(
        "\nListo. Los 6 agentes de la Fase 6 están construidos. Siguiente paso: "
        "Fase 7 de Guia_Construccion.md (graph/nodes.py, edges.py, workflow.py)."
    )
