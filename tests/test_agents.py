"""Pruebas unitarias de cada agente individual (Fase 8 de Guia_Construccion.md).

Qué hace:
    Prueba cada agente con el LLM mockeado (invoke_structured reemplazado por
    un valor canned), reutilizando los mismos estados de prueba escritos a
    mano que ya usaba cada agente en su propio smoke test (Fase 6). Verifica
    tres cosas por agente: (1) precondiciones rotas fallan con ValueError
    ANTES de tocar el LLM, (2) el agente arma el dict de update parcial
    esperado a partir de lo que devuelve el LLM, y (3) los campos calculados
    en Python (fuentes_consultadas, aprobado, guardrails del Reviewer) se
    recalculan correctamente y no confían ciegamente en lo que "dijo" el LLM.

Responsabilidad dentro del sistema:
    Da señal rápida y gratuita (sin gastar cuota de ningún proveedor LLM) de
    que la lógica propia de cada agente sigue funcionando, sin depender de
    que un modelo gratuito esté disponible o responda de forma determinista.

Decisiones (Fase 8 de Guia_Construccion.md):
    - Se mockea agents.<modulo>.invoke_structured, no ChatOpenAI ni la red:
      es el único punto de entrada al LLM que cada agente usa (agents/llm_factory.py),
      así que interceptarlo ahí cubre a los 6 agentes con el mismo patrón sin
      tener que simular HTTP.
    - Los retrievers RAG (architect/security/developer/testing) SÍ se dejan
      reales: son búsquedas locales contra Chroma (rag/ingestion.py ya
      corrido), sin costo de red ni de tokens — mockearlos agregaría
      complejidad sin ahorrar nada. Si rag/ingestion.py no se corrió todavía,
      estos tests fallan con un mensaje claro (ver test_*_agent_rag_vacio),
      igual que ya advertían los smoke tests de la Fase 6.
    - developer_agent y testing_agent NO se prueban de punta a punta acá
      (requieren un ciclo ReAct completo contra un servidor MCP real vía
      subprocess + REPO_TARGET_PATH poblado — eso es justamente lo que cubren
      los escenarios de tests/test_scenarios.py, marcados @pytest.mark.integration).
      Acá se prueban sus partes deterministas y aisladas: la precondición de
      entrada, y las funciones puras de agents/mcp_tools.py y
      testing_agent._aggregate_run_tests que calculan los campos que
      "nunca se le piden de memoria al LLM" (ver AGENTS.md).
"""

import asyncio

import pytest

import agents.architect_agent as architect_mod
import agents.product_agent as product_mod
import agents.reviewer_agent as reviewer_mod
import agents.security_agent as security_mod
from agents.architect_agent import ArchitectureProposal, TechnicalDecision, architect_agent
from agents.developer_agent import developer_agent
from agents.mcp_tools import (
    FileChange,
    apply_file_changes,
    invoke_with_retry,
    result_text,
    summarize_args,
    track_file_change,
    tool_to_openai_schema,
)
from agents.product_agent import ProductSpecification, product_agent
from agents.reviewer_agent import ReviewVerdict, _coerce_verdict, reviewer_agent
from agents.security_agent import SecurityFinding, SecurityReview, security_agent
from agents.testing_agent import _aggregate_run_tests
from agents.testing_agent import testing_agent as run_testing_agent
from graph.state import create_initial_state

REQUIREMENT = (
    "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
    "y fin, y que mi jefe directo apruebe o rechace la solicitud."
)


def _fake_invoke_structured(return_value):
    """Reemplazo de agents.llm_factory.invoke_structured: ignora schema/messages
    y devuelve return_value tal cual, sin llamar a ningún proveedor LLM."""

    def _fake(schema, messages, method="function_calling", temperature=0):
        return return_value

    return _fake


# ---------- product_agent ----------


def test_product_agent_falla_sin_llamar_al_llm_si_requirement_vacio(monkeypatch):
    llamado = []
    monkeypatch.setattr(product_mod, "invoke_structured", lambda *a, **k: llamado.append(1))

    state = create_initial_state("   ")
    with pytest.raises(ValueError, match="requirement"):
        product_agent(state)
    assert llamado == [], "no debería haber llamado al LLM con un requirement vacío"


def test_product_agent_arma_specification_y_messages(monkeypatch):
    fake = ProductSpecification(
        resumen="Resumen de prueba.",
        actores=["Empleado", "Jefe directo"],
        reglas_negocio=["Una regla."],
        criterios_aceptacion=["Un criterio."],
        riesgos=["Un riesgo."],
        supuestos=["Un supuesto."],
    )
    monkeypatch.setattr(product_mod, "invoke_structured", _fake_invoke_structured(fake))

    state = create_initial_state(REQUIREMENT)
    resultado = product_agent(state)

    assert resultado["specification"] == fake.model_dump()
    assert resultado["messages"] == [
        "product_agent: especificación generada (2 actor(es), 1 regla(s), 1 riesgo(s))."
    ]


# ---------- architect_agent ----------


def test_architect_agent_falla_sin_llamar_al_llm_si_specification_vacia(monkeypatch):
    llamado = []
    monkeypatch.setattr(architect_mod, "invoke_structured", lambda *a, **k: llamado.append(1))

    state = create_initial_state(REQUIREMENT)  # specification queda {} por defecto
    with pytest.raises(ValueError, match="specification"):
        architect_agent(state)
    assert llamado == []


def test_architect_agent_sobreescribe_fuentes_con_las_del_retriever_real(monkeypatch):
    fake = ArchitectureProposal(
        resumen="Propuesta de prueba.",
        stack=["ASP.NET Core"],
        componentes=["Endpoint de creación"],
        decisiones_tecnicas=[
            TechnicalDecision(decision="Usar EF Core.", justificacion="Ya es el ORM del equipo.", trade_offs="Curva de aprendizaje.")
        ],
        plan_alto_nivel=["Implementar entidad", "Implementar endpoint"],
        riesgos_tecnicos=["Un riesgo técnico."],
        fuentes_consultadas=["esto-no-deberia-sobrevivir.md"],  # el LLM "alucina" una fuente
    )
    monkeypatch.setattr(architect_mod, "invoke_structured", _fake_invoke_structured(fake))

    state = create_initial_state(REQUIREMENT)
    state["specification"] = {
        "resumen": "Flujo de vacaciones.",
        "reglas_negocio": ["Un jefe no puede aprobar su propia solicitud."],
    }

    resultado = architect_agent(state)

    assert resultado["architecture"]["stack"] == ["ASP.NET Core"]
    # la fuente inventada por el LLM no debe sobrevivir: se recalcula desde el retriever real
    assert "esto-no-deberia-sobrevivir.md" not in resultado["architecture"]["fuentes_consultadas"]
    assert resultado["architecture"]["fuentes_consultadas"], (
        "el retriever de arquitectura debería haber devuelto al menos una fuente real "
        "(¿corriste 'python rag/ingestion.py'?)"
    )


# ---------- security_agent ----------


def test_security_agent_falla_sin_llamar_al_llm_si_architecture_vacia(monkeypatch):
    llamado = []
    monkeypatch.setattr(security_mod, "invoke_structured", lambda *a, **k: llamado.append(1))

    state = create_initial_state(REQUIREMENT)  # architecture queda {} por defecto
    with pytest.raises(ValueError, match="architecture"):
        security_agent(state)
    assert llamado == []


def test_security_agent_recalcula_aprobado_pese_a_que_el_llm_diga_true(monkeypatch):
    """El LLM dice aprobado=True, pero deja un hallazgo 'critica': aprobado debe
    quedar en False. Es la misma garantía que documenta AGENTS.md: 'aprobado'
    nunca se le pide de memoria al modelo, se recalcula en Python."""
    fake = SecurityReview(
        resumen="Revisión de prueba.",
        hallazgos=[
            SecurityFinding(
                severidad="critica",
                categoria_owasp="A01:2021 - Control de Acceso Roto",
                descripcion="Hallazgo simulado.",
                recomendacion="Corregir la validación.",
            )
        ],
        riesgos_aceptados=[],
        fuentes_consultadas=[],
        aprobado=True,  # el LLM se equivoca "por ser amable"
    )
    monkeypatch.setattr(security_mod, "invoke_structured", _fake_invoke_structured(fake))

    state = create_initial_state(REQUIREMENT)
    state["architecture"] = {"resumen": "API REST.", "componentes": ["Endpoint de aprobación"]}

    resultado = security_agent(state)

    assert resultado["security_review"]["aprobado"] is False


# ---------- developer_agent / testing_agent: precondición + funciones puras ----------


def test_developer_agent_falla_sin_architecture():
    state = create_initial_state(REQUIREMENT)  # architecture queda {} por defecto
    with pytest.raises(ValueError, match="architecture"):
        developer_agent(state)


def test_testing_agent_falla_sin_implementation():
    state = create_initial_state(REQUIREMENT)  # implementation queda {} por defecto
    with pytest.raises(ValueError, match="implementation"):
        run_testing_agent(state)


def test_aggregate_run_tests_suma_y_exige_total_mayor_a_cero():
    llamadas = [
        {"passed": 2, "failed": 0, "skipped": 0, "total": 2, "command": "dotnet test A"},
        {"passed": 1, "failed": 1, "skipped": 0, "total": 2, "command": "dotnet test B"},
    ]
    agregado = _aggregate_run_tests(llamadas)
    assert agregado["passed"] == 3
    assert agregado["failed"] == 1
    assert agregado["total"] == 4
    assert agregado["aprobado"] is False  # hay 1 failed


def test_aggregate_run_tests_sin_llamadas_no_esta_aprobado():
    """'no se corrió ningún test' NO es lo mismo que 'los tests pasan' — ver AGENTS.md."""
    agregado = _aggregate_run_tests([])
    assert agregado["total"] == 0
    assert agregado["aprobado"] is False


def test_aggregate_run_tests_timeout_bloquea_aprobado():
    llamadas = [{"passed": 3, "failed": 0, "skipped": 0, "total": 3, "timed_out": True}]
    agregado = _aggregate_run_tests(llamadas)
    assert agregado["aprobado"] is False


def test_track_file_change_create_file_exitoso():
    tool_call = {"name": "create_file", "args": {"file_path": "a.cs", "content": "hola"}}
    resultado = track_file_change(tool_call, "creado", is_error=False)
    assert resultado == ("a.cs", "creado", "".join(__import__("difflib").unified_diff(
        [], "hola".splitlines(keepends=True), fromfile="a/a.cs", tofile="b/a.cs"
    )))


def test_track_file_change_ignora_llamadas_con_error():
    tool_call = {"name": "create_file", "args": {"file_path": "a.cs", "content": "hola"}}
    assert track_file_change(tool_call, "ya existe", is_error=True) is None


def test_track_file_change_ignora_tools_de_lectura():
    tool_call = {"name": "read_file", "args": {"file_path": "a.cs"}}
    assert track_file_change(tool_call, "contenido", is_error=False) is None


def test_tool_to_openai_schema_mapea_campos():
    class FakeTool:
        name = "list_files"
        description = "Lista archivos."
        input_schema = {"type": "object", "properties": {}}

    schema = tool_to_openai_schema(FakeTool())
    assert schema == {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Lista archivos.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_result_text_concatena_bloques():
    class Chunk:
        def __init__(self, text):
            self.text = text

    class FakeResult:
        content = [Chunk("hola "), Chunk("mundo")]

    assert result_text(FakeResult()) == "hola mundo"


def test_summarize_args_prioriza_file_path():
    assert summarize_args({"file_path": "a.cs", "content": "x"}) == "a.cs"
    assert summarize_args({"subpath": ""}) == "(raíz)"
    assert summarize_args({"query": "class X"}) == "'class X'"


# ---------- invoke_with_retry (agents/mcp_tools.py) ----------


class _FakeStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"error {status_code}")
        self.status_code = status_code


def test_invoke_with_retry_reintenta_ante_429_y_devuelve_el_resultado(monkeypatch):
    monkeypatch.setattr("agents.mcp_tools.time.sleep", lambda segundos: None)

    llamadas = []

    class FakeLLM:
        def invoke(self, messages):
            llamadas.append(1)
            if len(llamadas) < 3:
                raise _FakeStatusError(429)
            return "respuesta ok"

    resultado = invoke_with_retry(FakeLLM(), ["mensaje"], max_intentos=3)
    assert resultado == "respuesta ok"
    assert len(llamadas) == 3


def test_invoke_with_retry_no_reintenta_errores_distintos_de_429(monkeypatch):
    monkeypatch.setattr("agents.mcp_tools.time.sleep", lambda segundos: None)

    llamadas = []

    class FakeLLM:
        def invoke(self, messages):
            llamadas.append(1)
            raise _FakeStatusError(404)

    with pytest.raises(_FakeStatusError):
        invoke_with_retry(FakeLLM(), ["mensaje"], max_intentos=3)
    assert len(llamadas) == 1, "un 404 no debería reintentarse"


def test_invoke_with_retry_lanza_si_se_agotan_los_intentos_con_429_persistente(monkeypatch):
    monkeypatch.setattr("agents.mcp_tools.time.sleep", lambda segundos: None)

    class FakeLLM:
        def invoke(self, messages):
            raise _FakeStatusError(429)

    with pytest.raises(_FakeStatusError):
        invoke_with_retry(FakeLLM(), ["mensaje"], max_intentos=2)


# ---------- apply_file_changes (agents/mcp_tools.py) — sesión MCP falsa, sin subproceso ----------


class _FakeToolResult:
    def __init__(self, text: str, is_error: bool = False):
        self.content = [type("Chunk", (), {"text": text})()]
        self.is_error = is_error


class _FakeSession:
    """Sesión MCP falsa: registra las llamadas y devuelve respuestas canned,
    sin levantar ningún subproceso real ni gastar tokens."""

    def __init__(self, respuestas: dict | None = None):
        self.llamadas: list[tuple[str, dict]] = []
        self._respuestas = respuestas or {}

    async def call_tool(self, name: str, args: dict):
        self.llamadas.append((name, args))
        texto, is_error = self._respuestas.get(name, ("ok", False))
        return _FakeToolResult(texto, is_error)


def test_apply_file_changes_crea_y_edita_sin_llm():
    fake = _FakeSession()
    cambios = [
        FileChange(file_path="a.cs", accion="crear", contenido="hola mundo", razon="archivo nuevo"),
        FileChange(file_path="b.cs", accion="editar", old_text="x", new_text="y", razon="fix"),
    ]

    creados, modificados, diffs, pasos = asyncio.run(apply_file_changes(fake, cambios, "Test"))

    assert creados == ["a.cs"]
    assert modificados == ["b.cs"]
    assert set(diffs) == {"a.cs", "b.cs"}
    assert len(pasos) == 2
    assert fake.llamadas[0] == ("create_file", {"file_path": "a.cs", "content": "hola mundo"})
    assert fake.llamadas[1] == ("update_file", {"file_path": "b.cs", "old_text": "x", "new_text": "y"})


def test_apply_file_changes_no_cuenta_los_cambios_que_fallan():
    fake = _FakeSession(respuestas={"create_file": ("ya existe", True)})
    cambios = [FileChange(file_path="a.cs", accion="crear", contenido="x", razon="r")]

    creados, modificados, diffs, pasos = asyncio.run(apply_file_changes(fake, cambios, "Test"))

    assert creados == []
    assert modificados == []
    assert diffs == {}
    assert "ERROR" in pasos[0]


def test_apply_file_changes_lista_vacia_no_llama_a_nada():
    fake = _FakeSession()
    creados, modificados, diffs, pasos = asyncio.run(apply_file_changes(fake, [], "Test"))
    assert (creados, modificados, diffs, pasos) == ([], [], {}, [])
    assert fake.llamadas == []


# ---------- reviewer_agent ----------


def test_reviewer_agent_falla_sin_implementation():
    state = create_initial_state(REQUIREMENT)  # implementation queda {} por defecto
    with pytest.raises(ValueError, match="implementation"):
        reviewer_agent(state)


def test_reviewer_agent_respeta_al_llm_sin_bloqueos(monkeypatch):
    fake = ReviewVerdict(
        status="APPROVED", resumen="Todo bien.", motivos=["Cumple criterios."],
        feedback="Queda verificado.", return_to=None,
    )
    monkeypatch.setattr(reviewer_mod, "invoke_structured", _fake_invoke_structured(fake))

    state = create_initial_state(REQUIREMENT)
    state["implementation"] = {"resumen": "Implementado.", "archivos_creados": ["a.cs"]}
    state["security_review"] = {"aprobado": True}
    state["test_results"] = {"aprobado": True}

    resultado = reviewer_agent(state)
    assert resultado["review"]["status"] == "APPROVED"
    assert resultado["review"]["return_to"] is None


@pytest.mark.parametrize(
    "security_aprobado,test_aprobado,return_to_esperado",
    [
        (False, True, "architect_agent"),
        (True, False, "developer_agent"),
    ],
)
def test_coerce_verdict_guardrail_ignora_al_llm_si_hay_bloqueo(
    security_aprobado, test_aprobado, return_to_esperado
):
    """_coerce_verdict es la pieza de más riesgo del pipeline (ver AGENTS.md):
    debe forzar REJECTED aunque el LLM diga APPROVED. Se prueba directo, sin
    LLM de por medio, exactamente como ya se validó a mano durante la
    construcción del agente (Escenario B del smoke test)."""
    verdict_del_llm = ReviewVerdict(
        status="APPROVED", resumen="El LLM se equivoca por ser amable.",
        motivos=["Todo se ve bien."], feedback="Aprobado.", return_to=None,
    )
    security_review = {"aprobado": security_aprobado}
    test_results = {"aprobado": test_aprobado}

    review = _coerce_verdict(verdict_del_llm, security_review, test_results)

    assert review["status"] == "REJECTED"
    assert review["return_to"] == return_to_esperado


def test_coerce_verdict_sin_bloqueos_respeta_al_llm():
    verdict_del_llm = ReviewVerdict(
        status="REJECTED", resumen="Falta cobertura.", motivos=["Un criterio sin test."],
        feedback="Agregar casos.", return_to="testing_agent",
    )
    review = _coerce_verdict(verdict_del_llm, {"aprobado": True}, {"aprobado": True})
    assert review["status"] == "REJECTED"
    assert review["return_to"] == "testing_agent"


def test_coerce_verdict_return_to_invalido_cae_a_developer_agent():
    verdict_del_llm = ReviewVerdict(
        status="REJECTED", resumen="Rechazado sin destino claro.", motivos=["Motivo vago."],
        feedback="Corregir.", return_to=None,
    )
    review = _coerce_verdict(verdict_del_llm, {"aprobado": True}, {"aprobado": True})
    assert review["status"] == "REJECTED"
    assert review["return_to"] == "developer_agent"
