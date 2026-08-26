"""Escenarios end-to-end reales usados para evaluar el sistema completo
(Fase 8 de Guia_Construccion.md).

Qué hace:
    Corre el grafo COMPLETO (graph/workflow.py) con LLM real (vía el fallback
    multi-proveedor de agents/llm_factory.py), RAG real y MCP real contra el
    repo clonado en REPO_TARGET_PATH — sin mocks, a diferencia de
    tests/test_agents.py y tests/test_graph.py. Cinco escenarios: tres
    "normales" (simple, complejo, ambiguo) y dos diseñados para que el
    pipeline SÍ encuentre un problema real (auto-aprobación tipo IDOR,
    condición de carrera), verificando que el sistema los detecta en vez de
    aprobarlos a ciegas.

Responsabilidad dentro del sistema:
    Es la única capa de pruebas que valida el sistema de punta a punta tal
    como lo usaría un usuario real — todo lo demás (test_agents.py,
    test_graph.py) prueba piezas aisladas con LLM mockeado.

Decisiones (Fase 8 de Guia_Construccion.md):
    - Marcados @pytest.mark.integration y excluidos por default (ver
      pytest.ini: addopts = -m "not integration"): cada escenario corre los
      6 agentes reales (varias llamadas LLM + RAG + un servidor MCP real
      corriendo `dotnet test`), lo que cuesta cuota real de API y tarda
      varios minutos por escenario. Correr esto en cada `pytest` suelto
      quemaría presupuesto sin necesidad; se corre explícitamente con
      `pytest -m integration` cuando de verdad se quiere validar el sistema
      completo (ej. antes de una entrega, no en cada iteración de código).
    - Se salta el módulo entero (no cada test individualmente) si no están
      los prerequisitos: ningún proveedor LLM configurado, o
      REPO_TARGET_PATH sin el repo MVC clonado. Fallar con un mensaje claro
      de "qué falta" es mejor que un traceback de MCP a mitad del test.
    - Assertions deliberadamente laxas en status exacto (APPROVED vs.
      REJECTED): con un LLM real y gratuito, el resultado exacto no es
      100% determinista de una corrida a otra. Lo que sí se verifica es que
      el pipeline llega a un veredicto válido y que, en los dos escenarios
      diseñados para fallar, el problema quedó señalado en ALGÚN punto del
      pipeline (hallazgos de seguridad, riesgos técnicos, o un REJECTED) —
      no que el texto exacto coincida con lo esperado.
    - No se limpia REPO_TARGET_PATH entre escenarios: developer_agent solo
      crea/edita archivos según lo que el LLM decida, y correr 5 escenarios
      reales sobre el mismo working copy es representativo de uso real
      (commits incrementales), no un environment descartable por test.
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from graph.state import create_initial_state
from graph.workflow import build_graph

load_dotenv()

_ANY_PROVIDER_KEY = (
    "NVIDIA_API_KEY", "GROQ_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY",
)


def _prerequisitos_faltantes() -> str | None:
    if not any(os.getenv(k) for k in _ANY_PROVIDER_KEY):
        return f"ningún proveedor LLM configurado ({', '.join(_ANY_PROVIDER_KEY)} vacíos en .env)"
    repo = os.getenv("REPO_TARGET_PATH", "")
    if not repo or not Path(repo).is_dir():
        return f"REPO_TARGET_PATH ({repo!r}) no existe; clona el repo MVC ahí antes de correr estos escenarios"
    return None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_prerequisitos_faltantes() is not None, reason=_prerequisitos_faltantes() or ""),
]


def _correr_escenario(requirement: str) -> dict:
    grafo = build_graph()
    estado_inicial = create_initial_state(requirement)
    resultado = grafo.invoke(estado_inicial)

    print(f"\n--- Métricas del escenario ---")
    print(f"requirement: {requirement[:80]}...")
    print(f"veredicto final: {resultado['review'].get('status')}")
    print(f"iteraciones usadas: {resultado.get('iteration')}")
    print(f"human_review_required: {resultado.get('human_review_required')}")
    print(f"hallazgos de seguridad: {len(resultado.get('security_review', {}).get('hallazgos', []))}")
    print(f"tests: {resultado.get('test_results', {}).get('passed')}/{resultado.get('test_results', {}).get('total')} pasaron")

    return resultado


# ---------- Escenarios "normales" ----------


def test_escenario_simple_solicitud_de_vacaciones_basica():
    resultado = _correr_escenario(
        "Como empleado quiero poder solicitar vacaciones indicando fecha de "
        "inicio y fin, y que mi jefe directo apruebe o rechace la solicitud."
    )
    assert resultado["review"].get("status") in ("APPROVED", "REJECTED")
    for campo in ("specification", "architecture", "implementation", "security_review", "test_results"):
        assert resultado.get(campo), f"{campo} debería haber quedado poblado"


def test_escenario_complejo_con_reglas_de_negocio_multiples():
    resultado = _correr_escenario(
        "Como empleado quiero solicitar vacaciones indicando fecha de inicio y "
        "fin. El sistema debe validar que no exceda mi saldo anual de días "
        "disponibles, que no se solape con otra solicitud aprobada, y requerir "
        "un mínimo de 5 días hábiles de anticipación. Mi jefe directo debe "
        "poder aprobar o rechazar con un motivo, y si rechaza debo recibir una "
        "notificación con ese motivo."
    )
    assert resultado["review"].get("status") in ("APPROVED", "REJECTED")
    assert resultado["specification"].get("reglas_negocio"), "debería haber extraído varias reglas de negocio"


def test_escenario_ambiguo_documenta_supuestos():
    resultado = _correr_escenario("Los empleados deberían poder pedir tiempo libre.")
    assert resultado["review"].get("status") in ("APPROVED", "REJECTED")
    # un requerimiento tan vago debería forzar al Product Agent a documentar
    # supuestos en vez de inventar detalles en silencio (ver ProductSpecification.supuestos)
    assert resultado["specification"].get("supuestos"), (
        "un requerimiento ambiguo debería dejar constancia de qué se asumió"
    )


# ---------- Escenarios diseñados para fallar ----------


def test_escenario_autoaprobacion_debe_quedar_senalado_como_riesgo():
    """IDOR / control de acceso roto: el requerimiento mismo pide una falla de
    seguridad. El sistema completo (Security Agent como mínimo) debería
    señalarlo — no necesariamente terminar en REJECTED (el LLM del Reviewer
    podría, con razón, exigir que se corrija antes de aprobar, o el propio
    Architect podría ya diseñarlo sin el hueco), pero el problema tiene que
    quedar documentado en algún punto del expediente."""
    resultado = _correr_escenario(
        "Como empleado quiero poder solicitar vacaciones y aprobar mi propia "
        "solicitud si mi jefe directo no responde en 24 horas, para no "
        "quedarme bloqueado esperando."
    )
    hallazgos = resultado.get("security_review", {}).get("hallazgos", [])
    riesgos_spec = resultado.get("specification", {}).get("riesgos", [])
    rejected = resultado["review"].get("status") == "REJECTED"

    assert hallazgos or riesgos_spec or rejected, (
        "el riesgo de auto-aprobación debería haber quedado señalado en "
        "security_review['hallazgos'], specification['riesgos'], o haber "
        "resultado en REJECTED — no pasó desapercibido en ningún lado"
    )


def test_escenario_condicion_de_carrera_debe_quedar_senalado_como_riesgo():
    """El requerimiento describe explícitamente un escenario de alta
    concurrencia sin mencionar ningún control — el Architect Agent (riesgos
    técnicos) o el Security Agent deberían señalar la condición de carrera."""
    resultado = _correr_escenario(
        "Como empleado quiero solicitar vacaciones. Es común que varios "
        "empleados del mismo equipo soliciten las mismas fechas al mismo "
        "tiempo y que el jefe directo apruebe varias solicitudes casi "
        "simultáneamente desde su celular; el sistema debe manejar bien ese "
        "volumen de aprobaciones concurrentes sobre el mismo período."
    )
    riesgos_tecnicos = resultado.get("architecture", {}).get("riesgos_tecnicos", [])
    hallazgos = resultado.get("security_review", {}).get("hallazgos", [])
    rejected = resultado["review"].get("status") == "REJECTED"

    assert riesgos_tecnicos or hallazgos or rejected, (
        "la condición de carrera de aprobaciones concurrentes debería haber "
        "quedado señalada en architecture['riesgos_tecnicos'], "
        "security_review['hallazgos'], o haber resultado en REJECTED"
    )
