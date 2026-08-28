"""Agente de Testing (testing_agent).

Qué hace:
    Lee la `implementation` que dejó el Developer Agent (y la especificación
    funcional, para los criterios de aceptación), consulta el RAG de testing
    (knowledge/testing/testing-strategy.md) y en dos fases contra el servidor
    MCP real: (1) explora los proyectos de test existentes y (2) ejecuta un
    plan estructurado y compacto (qué casos agregar, qué `run_tests` correr)
    — devolviendo pass/fail real, no inventado.

Responsabilidad dentro del sistema:
    Verifica objetivamente que la implementación cumple lo esperado y reporta
    resultados reales de ejecución al resto del pipeline (Reviewer, sobre
    todo). Quinto eslabón del pipeline (Product -> Architect -> Developer ->
    Security -> Testing -> Reviewer, ver README.md).

Decisiones (Fase 6 de Guia_Construccion.md, agente 5/6):
    - Nueva dependencia: la tool MCP `run_tests` (mcp_server/server.py). Corre
      `dotnet test` real vía subprocess y parsea las líneas de resumen que
      dotnet imprime por proyecto de test.
    - Depende de `implementation` (obligatoria); `specification` y
      `security_review` son opcionales, como contexto adicional.
    - REDISEÑO: mismo patrón que developer_agent.py — "planificar" separado
      de "ejecutar" (agents/mcp_tools.py -> run_exploration_loop /
      apply_file_changes), en vez de un único ciclo ReAct abierto que
      mezclaba exploración, creación de casos nuevos y `run_tests` en la
      misma conversación creciente (causa real de 413 "Request too large" y
      de que el modelo alucinara tools a mitad de una conversación larga).
      Ahora:
        1. run_exploration_loop() explora CORTO (MAX_EXPLORATION_TURNS) con
           SOLO tools de lectura, para encontrar el proyecto de test real.
        2. invoke_structured() pide un `TestingPlan` compacto: qué casos
           nuevos agregar (si hace falta) y qué `run_tests` correr.
        3. El código ejecuta ese plan: primero los casos nuevos
           (apply_file_changes, sin LLM), después cada `run_tests` del plan,
           iterando en Python — sin ninguna llamada LLM adicional.
    - `run_tests_calls` del plan SIEMPRE incluye al menos una corrida real:
      si el LLM no propuso ninguna (a pesar de que el prompt lo exige), el
      código agrega una corrida por defecto sobre la raíz del repo — la
      obligación de correr tests de verdad queda garantizada por CÓDIGO, no
      solo por texto del prompt, mismo principio que ya aplica todo el resto
      del sistema ("no confiar en la memoria/obediencia del modelo para
      hechos verificables").
    - `passed`/`failed`/`skipped`/`total`/`aprobado` se calculan en Python
      (`_aggregate_run_tests`, sin cambios) a partir de los resultados REALES
      de cada `run_tests` — nunca se le pide al LLM que los reporte de
      memoria.
    - `aprobado` exige AL MENOS una llamada real a `run_tests`, cero
      `failed`, `total > 0` y ningún timeout: un `total == 0` (no se
      encontraron/corrieron tests) NO cuenta como aprobado, aunque
      `failed == 0` — "no hay tests que fallen" no es lo mismo que "los
      tests pasan".
    - `casos_generados` ahora es directamente `archivos_creados` (los
      archivos que `apply_file_changes()` realmente creó) en vez de una
      lista redactada por el LLM en una llamada de resumen aparte — mismo
      principio de no confiar en la memoria del modelo, llevado un paso más
      allá: ni siquiera se le pregunta, se usa el hecho verificable directo.
    - Cliente LLM vía agents/llm_factory.py (build_llm/invoke_structured).
    - print() por fase/turno/cambio: mismo motivo que developer_agent.py —
      sin esto, la terminal queda en silencio total durante la corrida.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    # Permite "python agents/testing_agent.py" como script suelto (Fase 6 de
    # la guía): sin esto, sys.path solo contendría agents/, no la raíz del
    # repo, y "from graph.state import ..." fallaría con ModuleNotFoundError.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_factory import build_llm, invoke_structured
from agents.mcp_tools import (
    REPO_ROOT,
    FileChange,
    apply_file_changes,
    mcp_server_params,
    result_text,
    run_exploration_loop,
    summarize_exploration,
)
from graph.state import EngineeringState, create_initial_state
from observability.langfuse_config import flush_traces, observe
from rag.retrievers import get_testing_retriever

load_dotenv()

__all__ = ["TestingPlan", "testing_agent"]

MAX_EXPLORATION_TURNS = 5  # cota de la fase de exploración (solo lectura);
# la ejecución (casos nuevos + run_tests) no tiene ciclo de turnos propio.

_EXPLORATION_PROMPT = """\
Eres un QA engineer senior de un estudio de ingeniería. Recibís lo que el
Developer Agent implementó (archivos creados/modificados, resumen), los
criterios de aceptación de la especificación original, y fragmentos reales
de la estrategia de testing del equipo.

Estás en la FASE DE EXPLORACIÓN, no en la de ejecución: en este turno solo
tenés disponibles tools de LECTURA (list_files, read_file, search_code) — no
existe create_file, update_file ni run_tests en esta fase, ni ninguna otra
tool bajo ningún nombre (ej. NO existe "print_tree" ni variantes; list_files()
ya da el árbol completo y recursivo en un solo llamado).

Tu único objetivo es encontrar el/los proyecto(s) de test reales relacionados
con lo que tocó el Developer Agent (normalmente un proyecto `*.Tests` por
capa, ver la estrategia de testing provista abajo) y, si vas a proponer casos
nuevos, el contenido EXACTO de los archivos que vayas a editar.

No repitas una tool con los mismos argumentos si ya tenés esa información. Si
una llamada falla (ERROR), NUNCA la reintentes igual — cambiá de estrategia.
Cuando tengas contexto suficiente, dejá de pedir tools y respondé con texto
breve confirmando que estás listo. Responde siempre en español.
"""

_PLAN_SYSTEM_PROMPT = """\
Eres un QA engineer senior de un estudio de ingeniería. En este paso tu
único trabajo es análisis y planificación — no ejecutás nada directamente,
solo respondés con el plan estructurado que se te pide, basado en la
información que se te da a continuación.
"""

_PLAN_PROMPT = """\
Con base en la exploración ya realizada (arriba), generá el plan de verificación:

- `run_tests_calls`: AL MENOS una corrida real, apuntando al proyecto o
  carpeta de test relevante que encontraste explorando (o a la raíz si no
  estás seguro). Esto es obligatorio.
- `casos_a_agregar`: SOLO si detectaste un criterio de aceptación relevante
  sin cobertura de test existente — seguí la pirámide de pruebas y las
  convenciones del equipo. Si lo existente ya cubre lo relevante, dejalo
  vacío; no agregues casos por agregar.
- Si se te proveyeron hallazgos de seguridad, priorizá que los casos que
  agregues (o los `run_tests_calls` que elijas) cubran esos casos de abuso
  (auto-aprobación, IDOR, forced browsing, escalación de privilegios) antes
  que casos genéricos.

Para cada entrada de `casos_a_agregar` (mismo formato que un cambio de
archivo): accion="crear" con `contenido` completo, o accion="editar" con
`old_text` EXACTO (el que vos mismo leíste) y `new_text`.

`resumen` describe qué vas a verificar y cómo; `hallazgos` son criterios sin
cobertura o fallos relevantes a destacar (vacío si no hay); `notas` son
limitaciones conocidas o seguimientos sugeridos (vacío si no hay).
"""


class RunTestsCall(BaseModel):
    subpath: str = Field(default="", description="Carpeta/proyecto de test a correr; vacío para la raíz del repo.")
    filter: str = Field(default="", description="Filtro opcional de dotnet test; vacío para correr todos los tests del subpath.")


class TestingPlan(BaseModel):
    """Plan estructurado y compacto de verificación — reemplaza al ciclo
    abierto de tool-calling: el código ejecuta esta lista directo contra el
    MCP (casos nuevos + run_tests), sin ningún LLM adicional."""

    casos_a_agregar: list[FileChange] = Field(
        default_factory=list,
        description="Casos de prueba nuevos a crear/editar; vacío si lo existente ya cubre lo relevante.",
    )
    run_tests_calls: list[RunTestsCall] = Field(
        default_factory=list,
        description="Corridas de run_tests a ejecutar; el código garantiza al menos una aunque esta lista venga vacía.",
    )
    resumen: str = Field(..., description="Resumen breve (2-4 frases) de qué se va a verificar y cómo.")
    hallazgos: list[str] = Field(
        default_factory=list,
        description="Criterios de aceptación sin cobertura, o fallos relevantes a destacar; vacío si no hubo.",
    )
    notas: list[str] = Field(
        default_factory=list, description="Limitaciones conocidas o seguimientos sugeridos; vacío si no hubo."
    )


def _format_context(docs) -> str:
    """Arma el bloque de contexto RAG a partir de los chunks recuperados."""
    if not docs:
        return "(el retriever de testing no devolvió resultados para esta consulta)"
    partes = []
    for doc in docs:
        fuente = doc.metadata.get("source", "desconocida")
        header = doc.metadata.get("header", "")
        etiqueta = f"[{fuente}{' - ' + header if header else ''}]"
        partes.append(f"{etiqueta}\n{doc.page_content}")
    return "\n\n".join(partes)


def _aggregate_run_tests(run_tests_calls: list[dict]) -> dict:
    """Suma passed/failed/skipped/total de todas las corridas reales de run_tests.

    aprobado exige al menos una corrida real, cero fallos, total > 0 y ningún
    timeout — ver la decisión correspondiente en el docstring del módulo.
    """
    totals = {"passed": 0, "failed": 0, "skipped": 0, "total": 0}
    algun_timeout = False
    comandos: list[str] = []
    for llamada in run_tests_calls:
        for clave in totals:
            totals[clave] += int(llamada.get(clave, 0))
        algun_timeout = algun_timeout or bool(llamada.get("timed_out"))
        if llamada.get("command"):
            comandos.append(llamada["command"])

    aprobado = bool(run_tests_calls) and totals["failed"] == 0 and totals["total"] > 0 and not algun_timeout
    return {**totals, "aprobado": aprobado, "comandos_ejecutados": comandos}


async def _plan_and_apply(
    mensaje_usuario: str,
) -> tuple[list[str], list[str], list[dict], list[str], str, list[str], list[str]]:
    """Explora corto, pide un plan estructurado y lo ejecuta sin LLM adicional.

    Devuelve (archivos_creados, archivos_modificados, run_tests_calls, pasos, resumen, hallazgos, notas).
    """
    async with stdio_client(mcp_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            llm = build_llm()

            messages = await run_exploration_loop(
                session, llm, _EXPLORATION_PROMPT, mensaje_usuario,
                MAX_EXPLORATION_TURNS, "Testing Agent",
            )

            print("      [Testing Agent] generando plan de verificación...")
            resumen_exploracion = summarize_exploration(messages)
            plan_context = (
                f"Contexto original:\n{mensaje_usuario}\n\n"
                f"--- Exploración ya realizada ---\n{resumen_exploracion}\n\n{_PLAN_PROMPT}"
            )
            try:
                # Contexto NUEVO de 2 mensajes (System + Human), sin los
                # AIMessage con tool_calls de la exploración — ver la decisión
                # en agents/mcp_tools.py::summarize_exploration.
                plan = invoke_structured(
                    TestingPlan,
                    [SystemMessage(content=_PLAN_SYSTEM_PROMPT), HumanMessage(content=plan_context)],
                )
            except Exception as error:
                # Mismo criterio que developer_agent.py: si ningún proveedor
                # logró devolver el plan, no debe tumbar todo el pipeline —
                # se sigue con un resultado vacío y una nota explícita.
                print(f"      [Testing Agent] ALERTA - no se pudo generar el plan de verificación: {error}")
                return [], [], [], [f"⚠ no se pudo generar el plan de verificación: {error}"], (
                    "No se pudo verificar nada: todos los proveedores LLM fallaron "
                    "al pedir el plan de verificación."
                ), [], []

            corridas = plan.run_tests_calls or [RunTestsCall(subpath="", filter="")]
            print(
                f"      [Testing Agent] plan con {len(plan.casos_a_agregar)} caso(s) nuevo(s) y "
                f"{len(corridas)} corrida(s) de run_tests; ejecutando..."
            )

            archivos_creados, archivos_modificados, _diffs, pasos = await apply_file_changes(
                session, plan.casos_a_agregar, "Testing Agent"
            )

            run_tests_calls: list[dict] = []
            for llamada in corridas:
                print(f"      [Testing Agent] -> run_tests(subpath={llamada.subpath!r}, filter={llamada.filter!r})")
                result = await session.call_tool(
                    "run_tests", {"subpath": llamada.subpath, "filter": llamada.filter}
                )
                text = result_text(result)
                is_error = bool(getattr(result, "is_error", False))
                estado = "ERROR" if is_error else "OK"
                print(f"      [Testing Agent]    {estado}")
                pasos.append(f"run_tests(subpath={llamada.subpath!r}) -> {estado}")
                if not is_error:
                    try:
                        run_tests_calls.append(json.loads(text))
                    except json.JSONDecodeError:
                        pass

            return (
                archivos_creados,
                archivos_modificados,
                run_tests_calls,
                pasos,
                plan.resumen,
                plan.hallazgos,
                plan.notas,
            )


@observe(name="testing_agent")
def testing_agent(state: EngineeringState) -> dict:
    """Verifica state["implementation"] corriendo tests reales sobre el repo vía MCP.

    Devuelve un update parcial del estado: {"test_results": {...}, "messages": [...]}.
    """
    implementation = state["implementation"]
    if not implementation:
        raise ValueError(
            "state['implementation'] está vacío; corre developer_agent antes de testing_agent."
        )
    specification = state.get("specification", {})
    security_review = state.get("security_review", {})

    consulta_rag = " ".join(
        [
            implementation.get("resumen", ""),
            *specification.get("criterios_aceptacion", []),
            *implementation.get("archivos_creados", []),
            *implementation.get("archivos_modificados", []),
            *(h.get("descripcion", "") for h in security_review.get("hallazgos", [])),
        ]
    ).strip()
    if not consulta_rag:
        consulta_rag = "estrategia de testing para la implementación provista"

    retriever = get_testing_retriever()
    docs = retriever.invoke(consulta_rag)
    contexto = _format_context(docs)
    fuentes = sorted({doc.metadata.get("source") for doc in docs if doc.metadata.get("source")})

    mensaje_usuario = (
        "Criterios de aceptación de la especificación original (JSON):\n"
        f"{json.dumps(specification.get('criterios_aceptacion', []), ensure_ascii=False, indent=2)}\n\n"
        "Hallazgos de la revisión de seguridad, si los hay (JSON):\n"
        f"{json.dumps(security_review.get('hallazgos', []), ensure_ascii=False, indent=2)}\n\n"
        "Lo que implementó el Developer Agent (JSON):\n"
        f"{json.dumps(implementation, ensure_ascii=False, indent=2)}\n\n"
        "Contexto recuperado de la estrategia de testing interna:\n"
        f"{contexto}"
    )

    archivos_creados, archivos_modificados, run_tests_calls, pasos, resumen, hallazgos, notas = asyncio.run(
        _plan_and_apply(mensaje_usuario)
    )

    test_results = {
        "resumen": resumen,
        "casos_generados": archivos_creados,
        "hallazgos": hallazgos,
        "notas": notas,
        **_aggregate_run_tests(run_tests_calls),
        "archivos_creados": archivos_creados,
        "archivos_modificados": archivos_modificados,
        "pasos_seguidos": pasos,
        "fuentes_consultadas": fuentes,
    }

    return {
        "test_results": test_results,
        "messages": [
            f"testing_agent: {test_results['passed']} passed, {test_results['failed']} failed, "
            f"{test_results['skipped']} skipped de {test_results['total']} total "
            f"({len(run_tests_calls)} corrida(s) de run_tests), aprobado={test_results['aprobado']}, "
            f"fuentes RAG: {', '.join(fuentes) if fuentes else 'ninguna'})."
        ],
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("Fase 6 (agente 5/6) — smoke test de agents/testing_agent.py")

    print("1. Verificando OPENROUTER_API_KEY en el entorno...")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit(
            "   ERROR - OPENROUTER_API_KEY no configurada. Completa tu .env "
            "(copia .env.example) antes de correr esta prueba en vivo."
        )
    print("   OK - variable presente.")

    print("2. Verificando que REPO_TARGET_PATH exista (clon local del MVC)...")
    repo_target = os.getenv("REPO_TARGET_PATH", "")
    if not repo_target or not (REPO_ROOT / repo_target).resolve().is_dir():
        raise SystemExit(
            f"   ERROR - REPO_TARGET_PATH={repo_target!r} no existe. Clona tu repo MVC "
            "en esa ruta (o ajusta REPO_TARGET_PATH en .env) antes de correr esta prueba."
        )
    print(f"   OK - {repo_target!r} existe.")

    print("3. Armando un estado con implementation de prueba (simulando al Developer Agent)...")
    requerimiento_ejemplo = sys.argv[1] if len(sys.argv) > 1 else (
        "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
        "y fin, y que mi jefe directo apruebe o rechace la solicitud."
    )
    state = create_initial_state(requerimiento_ejemplo)
    if len(sys.argv) > 1:
        print(
            "   (nota: requirement personalizado; la implementation de prueba de abajo "
            "sigue siendo la del ejemplo de vacaciones, no se deriva de tu texto)"
        )
    state["specification"] = {
        "resumen": "Flujo de solicitud y aprobación de vacaciones entre empleado y aprobador.",
        "actores": ["Empleado", "Aprobador"],
        "reglas_negocio": ["Un aprobador no puede aprobar su propia solicitud."],
        "criterios_aceptacion": [
            "Los tests del proyecto Smoke.Tests deben poder ejecutarse y reportar su resultado real.",
        ],
        "riesgos": ["Auto-aprobación si el aprobador y el autor son el mismo usuario."],
        "supuestos": [],
    }
    state["implementation"] = {
        "resumen": "Se agregó un proyecto de test 'Smoke.Tests' con 3 casos (2 deberían pasar, 1 está diseñado para fallar) para validar el ciclo real de ejecución de pruebas.",
        "notas": [],
        "archivos_creados": ["Smoke.Tests/UnitTest1.cs", "Smoke.Tests/Smoke.Tests.csproj"],
        "archivos_modificados": [],
        "diff": "(no aplica para este smoke test)",
        "pasos_seguidos": [],
        "fuentes_consultadas": [],
    }

    print("4. Verificando que el retriever de testing devuelva resultados (rag/ ya poblado)...")
    docs_check = get_testing_retriever().invoke(state["implementation"]["resumen"])
    if not docs_check:
        raise SystemExit(
            "   ERROR - 0 resultados del retriever de testing. Corre primero "
            "'python rag/ingestion.py' para poblar el vector store."
        )
    print(f"   OK - {len(docs_check)} fragmento(s) recuperado(s).")

    print("5. Invocando testing_agent (llamadas reales al LLM, al servidor MCP y a 'dotnet test')...")
    resultado = testing_agent(state)

    print("6. Resultados de testing generados:")
    print(json.dumps(resultado["test_results"], indent=2, ensure_ascii=False))

    tr = resultado["test_results"]
    checks = {
        "pasos_seguidos": tr.get("pasos_seguidos"),
        "comandos_ejecutados": tr.get("comandos_ejecutados"),
    }
    for campo, valor in checks.items():
        flag = "OK" if valor else "ALERTA - vacío"
        print(f"   {flag} - {campo}: {len(valor) if valor else 0} elemento(s)")
    print(
        f"   {'OK' if tr.get('total', 0) > 0 else 'ALERTA'} - total de tests corridos: {tr.get('total', 0)}"
    )

    print("7. Forzando flush de traces a Langfuse...")
    flush_traces()

    print("\nListo. Siguiente paso: agents/reviewer_agent.py (Fase 6, agente 6/6).")
