"""Agente de Testing (testing_agent).

Qué hace:
    Lee la `implementation` que dejó el Developer Agent (y la especificación
    funcional, para los criterios de aceptación), consulta el RAG de testing
    (knowledge/testing/testing-strategy.md) y ejecuta un ciclo de tool-calling
    contra el servidor MCP propio para explorar los proyectos de test del
    repositorio real, opcionalmente agregar casos de prueba que falten, y
    correr `run_tests` — devolviendo pass/fail real, no inventado.

Responsabilidad dentro del sistema:
    Verifica objetivamente que la implementación cumple lo esperado y reporta
    resultados reales de ejecución al resto del pipeline (Reviewer, sobre
    todo). Quinto eslabón del pipeline (Product -> Architect -> Developer ->
    Security -> Testing -> Reviewer, ver README.md).

Decisiones (Fase 6 de Guia_Construccion.md, agente 5/6):
    - Nueva dependencia: la tool MCP `run_tests`, agregada a
      mcp_server/server.py en este mismo paso (no existía — su docstring
      decía explícitamente "run_tests y get_diff se agregarán después").
      Corre `dotnet test` real vía subprocess y parsea las líneas de resumen
      que dotnet imprime por proyecto de test.
    - Reutiliza el mismo patrón de ciclo ReAct que developer_agent.py
      (agente 4/6): conexión MCP por protocolo real, tools bindeadas al LLM
      sin adapter externo. La plomería común (conectar, convertir tools,
      trackear create_file/update_file) ya vive en agents/mcp_tools.py,
      extraída precisamente para este segundo caso de uso.
    - Depende de `implementation` (obligatoria); `specification` y
      `security_review` son opcionales, como contexto adicional. A
      diferencia de developer_agent.py (que corre ANTES que Security y por
      eso no puede depender de `security_review`), el pipeline real del
      grafo es Product -> Architect -> Developer -> Security -> Testing ->
      Reviewer (ver README.md): cuando Testing corre, Security ya corrió, así
      que `security_review` normalmente sí existe. Se usa para priorizar los
      "casos de abuso" que `knowledge/testing/testing-strategy.md` pide
      explícitamente cubrir con test (auto-aprobación, IDOR, forced
      browsing...), pero se lee con `state.get(...)` y no con `state[...]`
      para que el agente siga siendo probable de forma aislada (Fase 6) con
      solo una `implementation` de prueba, sin necesitar simular también un
      `security_review`.
    - `passed`/`failed`/`skipped`/`total`/`aprobado` se calculan en Python a
      partir de los resultados REALES de cada llamada a `run_tests` que el
      LLM ejecutó — nunca se le pide al LLM que reporte estos números de
      memoria. Mismo principio que `archivos_creados`/`archivos_modificados`
      en developer_agent.py, llevado a la parte más crítica de este agente:
      un número de tests pasados/fallidos inventado por el LLM sería
      particularmente engañoso para el Reviewer, que lo usa como señal
      objetiva.
    - El prompt exige explícitamente invocar `run_tests` al menos una vez
      antes de terminar — a diferencia de developer_agent.py, donde el
      objetivo (crear/editar archivos) es visible en pasos_seguidos aunque el
      LLM se distraiga, acá un ciclo que solo explora sin nunca ejecutar
      `run_tests` produciría un reporte con `aprobado=False` y cero tests
      corridos, que es indistinguible de "no se pudo verificar nada" — hay
      que dejarlo así de explícito en las instrucciones para minimizar que
      pase con un modelo gratuito.
    - `aprobado` exige AL MENOS una llamada real a `run_tests`, cero
      `failed`, `total > 0` y ningún timeout: un `total == 0` (no se
      encontraron/corrieron tests) NO cuenta como aprobado, aunque
      `failed == 0` — "no hay tests que fallen" no es lo mismo que "los
      tests pasan".
    - Cliente LLM vía agents/llm_factory.py (build_llm), igual que los
      cuatro agentes anteriores.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    # Permite "python agents/testing_agent.py" como script suelto (Fase 6 de
    # la guía): sin esto, sys.path solo contendría agents/, no la raíz del
    # repo, y "from graph.state import ..." fallaría con ModuleNotFoundError.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_factory import build_llm
from agents.mcp_tools import (
    REPO_ROOT,
    mcp_server_params,
    result_text,
    summarize_args,
    tool_to_openai_schema,
    track_file_change,
)
from graph.state import EngineeringState, create_initial_state
from observability.langfuse_config import flush_traces, observe
from rag.retrievers import get_testing_retriever

load_dotenv()

__all__ = ["TestingSummary", "testing_agent"]

MAX_TOOL_ITERATIONS = 12
_TOOL_RESULT_CHAR_LIMIT = 4000

_SYSTEM_PROMPT = """\
Eres un QA engineer senior de un estudio de ingeniería. Recibes lo que el
Developer Agent implementó (archivos creados/modificados, resumen), los
criterios de aceptación de la especificación original, y fragmentos reales
de la estrategia de testing del equipo. Tu trabajo es verificar de forma
OBJETIVA que la implementación funciona, ejecutando pruebas reales — nunca
afirmes un resultado de pruebas sin haberlo corrido.

Instrucciones:
- Explora el repositorio (list_files, read_file, search_code) para encontrar
  los proyectos de test relacionados con los archivos que tocó el Developer
  Agent — normalmente un proyecto `*.Tests` por cada capa (ver la estrategia
  de testing provista abajo).
- Si se te provee una revisión de seguridad con hallazgos, priorizá verificar
  con test los casos de abuso relacionados (auto-aprobación, IDOR, forced
  browsing, escalación de privilegios) antes que casos genéricos — son los
  que la estrategia de testing marca como obligatorios.
- Si detectás un criterio de aceptación relevante que claramente no tiene
  cobertura de test existente, podés agregar un caso de prueba nuevo con
  create_file/update_file, siguiendo la pirámide de pruebas y las
  convenciones del equipo (nivel correcto: unitaria/integración/E2E). No es
  obligatorio agregar tests nuevos si lo existente ya cubre lo relevante.
- Es OBLIGATORIO invocar la tool run_tests al menos una vez antes de
  terminar, apuntando al proyecto o carpeta de test relevante (o a la raíz
  si no estás seguro). No has terminado tu trabajo hasta que hayas corrido
  run_tests de verdad y visto un resultado real.
- Sé eficiente: tenés un número limitado de acciones. Explorá lo mínimo
  necesario para encontrar el proyecto de test correcto.
- Cuando termines, deja de pedir tools y responde con texto confirmando que
  terminaste.
- Responde siempre en español.
"""

_SUMMARY_PROMPT = """\
Ya ejecutaste las acciones necesarias sobre el repositorio en el turno
anterior, incluyendo al menos una corrida real de run_tests. Con base en
TODO lo que hiciste (no en lo que planeabas hacer), redacta un resumen breve
(2-4 frases) de qué se verificó, qué casos de prueba nuevos agregaste (si
hubo) y qué hallazgos relevantes surgieron (criterios de aceptación sin
cobertura, fallos inesperados). Si no hubo casos nuevos ni hallazgos, deja
esas listas vacías en vez de inventar contenido.
"""


class TestingSummary(BaseModel):
    """Parte de `test_results` que requiere criterio y redacta el LLM.

    El resto del dict (passed/failed/skipped/total/aprobado,
    archivos_creados, archivos_modificados, pasos_seguidos) se calcula en
    Python a partir de las tools realmente ejecutadas — ver decisiones en el
    docstring del módulo.
    """

    resumen: str = Field(
        ..., description="Resumen breve (2-4 frases) de qué se verificó y cómo."
    )
    casos_generados: list[str] = Field(
        default_factory=list,
        description="Casos de prueba nuevos que agregaste; vacío si solo ejecutaste tests existentes.",
    )
    hallazgos: list[str] = Field(
        default_factory=list,
        description="Criterios de aceptación sin cobertura de test, o fallos relevantes a destacar; vacío si no hubo.",
    )
    notas: list[str] = Field(
        default_factory=list,
        description="Limitaciones conocidas o seguimientos sugeridos; vacío si no hubo.",
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


async def _run_tool_loop(
    mensaje_usuario: str,
) -> tuple[list, list[str], list[str], list[dict], list[str]]:
    """Ejecuta el ciclo ReAct contra el servidor MCP real y devuelve lo ejecutado.

    Devuelve (messages, archivos_creados, archivos_modificados, run_tests_calls, pasos_seguidos).
    run_tests_calls es la lista de resultados (dict) de cada llamada real y
    exitosa a la tool run_tests, en el orden en que se ejecutaron.
    """
    async with stdio_client(mcp_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tool_schemas = [tool_to_openai_schema(t) for t in tools_result.tools]

            llm_with_tools = build_llm().bind_tools(tool_schemas)
            messages: list = [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=mensaje_usuario),
            ]

            archivos_creados: set = set()
            archivos_modificados: set = set()
            run_tests_calls: list[dict] = []
            pasos: list[str] = []

            for _ in range(MAX_TOOL_ITERATIONS):
                ai_message = llm_with_tools.invoke(messages)
                messages.append(ai_message)
                if not ai_message.tool_calls:
                    break
                for tool_call in ai_message.tool_calls:
                    result = await session.call_tool(
                        tool_call["name"], tool_call.get("args", {})
                    )
                    text = result_text(result)
                    is_error = bool(getattr(result, "is_error", False))

                    change = track_file_change(tool_call, text, is_error)
                    if change is not None:
                        file_path, bucket, _diff = change
                        (archivos_creados if bucket == "creado" else archivos_modificados).add(file_path)
                    if tool_call["name"] == "run_tests" and not is_error:
                        try:
                            run_tests_calls.append(json.loads(text))
                        except json.JSONDecodeError:
                            pass

                    estado = "ERROR" if is_error else "OK"
                    pasos.append(
                        f"{tool_call['name']}({summarize_args(tool_call.get('args', {}))}) -> {estado}"
                    )
                    messages.append(
                        ToolMessage(
                            content=text[:_TOOL_RESULT_CHAR_LIMIT],
                            tool_call_id=tool_call["id"],
                        )
                    )
            else:
                pasos.append(
                    f"⚠ se alcanzó MAX_TOOL_ITERATIONS={MAX_TOOL_ITERATIONS} sin que el modelo terminara por su cuenta."
                )

            return messages, sorted(archivos_creados), sorted(archivos_modificados), run_tests_calls, pasos


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

    messages, archivos_creados, archivos_modificados, run_tests_calls, pasos = asyncio.run(
        _run_tool_loop(mensaje_usuario)
    )

    structured_llm = build_llm().with_structured_output(TestingSummary, method="function_calling")
    summary = structured_llm.invoke(messages + [HumanMessage(content=_SUMMARY_PROMPT)])

    test_results = {
        "resumen": summary.resumen,
        "casos_generados": summary.casos_generados,
        "hallazgos": summary.hallazgos,
        "notas": summary.notas,
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
    state = create_initial_state(
        "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
        "y fin, y que mi jefe directo apruebe o rechace la solicitud."
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
