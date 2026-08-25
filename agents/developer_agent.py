
"""Agente Desarrollador (developer_agent).

Qué hace:
    Lee la propuesta técnica del Architect Agent (y la especificación
    funcional como contexto de negocio), consulta el RAG de desarrollo
    (knowledge/development/) y ejecuta un ciclo de tool-calling contra el
    servidor MCP propio (mcp_server/server.py) para explorar el repositorio
    real y crear/editar archivos, implementando el `plan_alto_nivel` de la
    arquitectura.

Responsabilidad dentro del sistema:
    Único agente autorizado a modificar código del repositorio objetivo.
    Todas sus operaciones sobre archivos pasan por las tools MCP (sandboxeadas
    a REPO_TARGET_PATH); nunca toca el filesystem directamente.

Decisiones (Fase 6 de Guia_Construccion.md, agente 4/6):
    - MCP sí, sin `security_review` como dependencia: según el pipeline real
      del grafo (README.md: Product -> Architect -> Developer -> Security ->
      Testing -> Reviewer) y el orden de campos de graph/state.py
      (`architecture` antes que `implementation`, `implementation` antes que
      `security_review`), el Developer Agent corre ANTES que el Security
      Agent — `state["security_review"]` todavía no existe en ese punto del
      flujo. Por eso este agente solo depende de `architecture` (obligatoria)
      y `specification` (opcional, como contexto de negocio), igual que la
      Fase 6 de la guía lo prueba: "pasarle una architecture de prueba".
    - Conexión MCP vía protocolo real (stdio_client + ClientSession), no
      llamando las funciones de mcp_server/server.py directamente: así el
      agente ejercita exactamente el mismo camino que un cliente MCP externo
      (igual que tests/test_mcp_protocol.py), y las tools quedan
      sandboxeadas del lado del servidor sin que este archivo conozca rutas
      absolutas del repo objetivo.
    - Las tools MCP listadas por el servidor (`session.list_tools()`) se
      convierten a schema OpenAI-tools al vuelo (`agents/mcp_tools.py ->
      tool_to_openai_schema`) y se bindean al LLM con `bind_tools()` — no se
      usa un adapter de terceros (`langchain-mcp-adapters` no está en
      requirements.txt): el schema de una `mcp.types.Tool`
      (name/description/input_schema) ya calza 1:1 con el formato
      `{"type": "function", "function": {...}}` que `bind_tools()` espera,
      así que no hace falta una dependencia extra.
    - La plomería de conexión MCP (lanzar el servidor, convertir tools,
      trackear create_file/update_file) vive en agents/mcp_tools.py, no en
      este archivo: se extrajo cuando testing_agent.py (agente 5/6) necesitó
      exactamente el mismo ciclo — mismo criterio que agents/llm_factory.py.
    - Ciclo ReAct manual con límite `MAX_TOOL_ITERATIONS`: en cada vuelta se
      invoca al LLM con tools bindeadas: si no pide más tool calls, corta.
      Si el límite se agota igual se sigue al resumen final (con lo hecho
      hasta ahí), dejando una nota explícita en `pasos_seguidos` en vez de
      fallar en silencio.
    - `archivos_creados`, `archivos_modificados`, `diff` y `pasos_seguidos`
      del `implementation` final se calculan en Python interceptando cada
      `create_file`/`update_file` que el LLM ejecuta realmente vía MCP — NUNCA
      se le pide al LLM que los recuerde de memoria al final del ciclo.
      Mismo principio que `fuentes_consultadas` en architect_agent.py y
      `aprobado` en security_agent.py, llevado un paso más allá: ni siquiera
      se le pide esa parte al LLM (no solo se le corrige después), porque
      tras varias vueltas de tool-calling un modelo gratuito tiende a
      recordar mal rutas exactas o el contenido preciso de cada diff.
    - El LLM sí redacta `resumen` y `notas` (desviaciones del plan,
      limitaciones, seguimientos sugeridos) vía `ImplementationSummary`
      (structured output), en una llamada aparte DESPUÉS del ciclo de tools:
      es la única parte del reporte final que requiere criterio, no un hecho
      verificable mecánicamente.
    - `diff` se arma con `difflib.unified_diff` sobre el fragmento
      reemplazado (`update_file`) o el archivo completo nuevo (`create_file`),
      no con una tool `get_diff` (todavía no existe en mcp_server/server.py,
      ver su docstring: "run_tests y get_diff se agregarán después").
    - Cliente LLM vía agents/llm_factory.py (build_llm), igual que los tres
      agentes anteriores.
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
    # Permite "python agents/developer_agent.py" como script suelto (Fase 6
    # de la guía): sin esto, sys.path solo contendría agents/, no la raíz del
    # repo, y "from graph.state import ..." fallaría con ModuleNotFoundError.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_factory import build_llm, invoke_structured
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
from rag.retrievers import get_development_retriever

load_dotenv()

__all__ = ["ImplementationSummary", "developer_agent"]

MAX_TOOL_ITERATIONS = 12
_TOOL_RESULT_CHAR_LIMIT = 4000

_SYSTEM_PROMPT = """\
Eres un ingeniero de software senior de un estudio de ingeniería. Recibes la
propuesta técnica que ya redactó el arquitecto (stack, componentes,
decisiones técnicas, plan de alto nivel) más fragmentos reales de las guías
internas de desarrollo del equipo (estándares de código y clean code). Tu
trabajo es EJECUTAR ese plan sobre el repositorio real, usando exclusivamente
las tools disponibles — nunca describas cambios sin aplicarlos.

Instrucciones:
- Antes de crear o editar nada, explora el repositorio real con list_files,
  read_file y search_code para confirmar rutas, convenciones y código
  existente. No asumas una estructura de carpetas que no verificaste.
- Sigue el `plan_alto_nivel` de la arquitectura en orden. Sigue las
  convenciones reales del proyecto (nombres en español, PascalCase,
  separación de capas Domain/Application/Infrastructure/Web) según el
  contexto de las guías de desarrollo provisto abajo, no una convención
  genérica inventada.
- Usa create_file solo para archivos nuevos. Usa update_file para editar
  archivos existentes, copiando el fragmento `old_text` EXACTO devuelto por
  read_file (mismos espacios e indentación) — si no es exacto, la tool falla
  sin tocar nada.
- Sé eficiente: tienes un número limitado de acciones. Explora lo mínimo
  necesario para tener certeza, y luego escribe. No repitas una tool con los
  mismos argumentos si ya tienes esa información.
- Si el contexto de las guías no cubre una decisión que necesitas tomar,
  sigue el patrón de código ya existente en el repositorio real antes que
  inventar una convención nueva.
- Cuando el plan esté completo, deja de pedir tools y responde con texto
  confirmando que terminaste.
- Responde siempre en español.
"""

_SUMMARY_PROMPT = """\
Ya ejecutaste las acciones necesarias sobre el repositorio en el turno
anterior. Con base en TODO lo que hiciste (no en lo que planeabas hacer),
redacta un resumen breve (2-4 frases) de qué se implementó y una lista de
notas relevantes: desviaciones del plan de arquitectura, limitaciones
conocidas, o seguimientos que el Testing Agent o el Reviewer deberían tener
en cuenta. Si no hubo desviaciones ni limitaciones, deja la lista de notas
vacía en vez de inventar una.
"""


class ImplementationSummary(BaseModel):
    """Parte de `implementation` que requiere criterio y redacta el LLM.

    El resto del dict `implementation` (archivos_creados, archivos_modificados,
    diff, pasos_seguidos) se calcula en Python a partir de las tools
    realmente ejecutadas — ver decisiones en el docstring del módulo.
    """

    resumen: str = Field(
        ..., description="Resumen breve (2-4 frases) de qué se implementó y cómo."
    )
    notas: list[str] = Field(
        default_factory=list,
        description="Desviaciones del plan, limitaciones conocidas o seguimientos sugeridos; vacío si no hubo.",
    )


def _format_context(docs) -> str:
    """Arma el bloque de contexto RAG a partir de los chunks recuperados."""
    if not docs:
        return "(el retriever de desarrollo no devolvió resultados para esta consulta)"
    partes = []
    for doc in docs:
        fuente = doc.metadata.get("source", "desconocida")
        header = doc.metadata.get("header", "")
        etiqueta = f"[{fuente}{' - ' + header if header else ''}]"
        partes.append(f"{etiqueta}\n{doc.page_content}")
    return "\n\n".join(partes)


def _format_diffs(diffs: dict) -> str:
    if not diffs:
        return "(sin cambios de archivo aplicados)"
    return "\n\n".join(diffs[path] for path in sorted(diffs))


async def _run_tool_loop(mensaje_usuario: str) -> tuple[list, list[str], list[str], dict, list[str]]:
    """Ejecuta el ciclo ReAct contra el servidor MCP real y devuelve lo ejecutado.

    Devuelve (messages, archivos_creados, archivos_modificados, diffs, pasos_seguidos).
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
            diffs: dict = {}
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
                        file_path, bucket, diff = change
                        diffs[file_path] = diff
                        (archivos_creados if bucket == "creado" else archivos_modificados).add(file_path)
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

            return messages, sorted(archivos_creados), sorted(archivos_modificados), diffs, pasos


@observe(name="developer_agent")
def developer_agent(state: EngineeringState) -> dict:
    """Implementa state["architecture"] sobre el repo real vía MCP.

    Devuelve un update parcial del estado: {"implementation": {...}, "messages": [...]}.
    """
    architecture = state["architecture"]
    if not architecture:
        raise ValueError(
            "state['architecture'] está vacío; corre architect_agent antes de developer_agent."
        )
    specification = state.get("specification", {})

    consulta_rag = " ".join(
        [
            architecture.get("resumen", ""),
            *architecture.get("componentes", []),
            *architecture.get("plan_alto_nivel", []),
        ]
    ).strip()
    if not consulta_rag:
        consulta_rag = "estándares de desarrollo para la arquitectura provista"

    retriever = get_development_retriever()
    docs = retriever.invoke(consulta_rag)
    contexto = _format_context(docs)
    fuentes = sorted({doc.metadata.get("source") for doc in docs if doc.metadata.get("source")})

    mensaje_usuario = (
        "Especificación funcional, como contexto de negocio (JSON):\n"
        f"{json.dumps(specification, ensure_ascii=False, indent=2)}\n\n"
        "Propuesta de arquitectura a implementar (JSON):\n"
        f"{json.dumps(architecture, ensure_ascii=False, indent=2)}\n\n"
        "Contexto recuperado de las guías internas de desarrollo:\n"
        f"{contexto}"
    )

    messages, archivos_creados, archivos_modificados, diffs, pasos = asyncio.run(
        _run_tool_loop(mensaje_usuario)
    )

    summary = invoke_structured(
        ImplementationSummary, messages + [HumanMessage(content=_SUMMARY_PROMPT)]
    )

    implementation = {
        "resumen": summary.resumen,
        "notas": summary.notas,
        "archivos_creados": archivos_creados,
        "archivos_modificados": archivos_modificados,
        "diff": _format_diffs(diffs),
        "pasos_seguidos": pasos,
        "fuentes_consultadas": fuentes,
    }

    return {
        "implementation": implementation,
        "messages": [
            f"developer_agent: implementación completada ({len(archivos_creados)} archivo(s) creado(s), "
            f"{len(archivos_modificados)} modificado(s), {len(pasos)} acción(es) MCP, "
            f"fuentes RAG: {', '.join(fuentes) if fuentes else 'ninguna'})."
        ],
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("Fase 6 (agente 4/6) — smoke test de agents/developer_agent.py")

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

    print("3. Armando un estado con architecture de prueba (simulando al Architect Agent)...")
    state = create_initial_state(
        "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
        "y fin, y que mi jefe directo apruebe o rechace la solicitud."
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
        "resumen": "Documentar en el repo el criterio de aceptación de auto-aprobación, sin tocar código de producción todavía.",
        "stack": ["Markdown"],
        "componentes": ["Nota de documentación de cambios"],
        "decisiones_tecnicas": [
            {
                "decision": "Crear _mcp_scratch/smoke_test_developer_agent.md documentando el criterio de auto-aprobación.",
                "justificacion": "Permite validar el ciclo MCP real (list_files/create_file) sin modificar código de producción del MVC.",
                "trade_offs": "No implementa código real; es solo para el smoke test de este agente.",
            }
        ],
        "plan_alto_nivel": [
            "Listar los archivos en la raíz del repositorio con list_files.",
            "Crear _mcp_scratch/smoke_test_developer_agent.md con una nota breve sobre la regla de auto-aprobación.",
        ],
        "riesgos_tecnicos": [],
        "fuentes_consultadas": [],
    }

    print("4. Verificando que el retriever de desarrollo devuelva resultados (rag/ ya poblado)...")
    docs_check = get_development_retriever().invoke(state["architecture"]["resumen"])
    if not docs_check:
        raise SystemExit(
            "   ERROR - 0 resultados del retriever de desarrollo. Corre primero "
            "'python rag/ingestion.py' para poblar el vector store."
        )
    print(f"   OK - {len(docs_check)} fragmento(s) recuperado(s).")

    print("5. Invocando developer_agent (llamadas reales al LLM y al servidor MCP)...")
    scratch_dir = (REPO_ROOT / repo_target).resolve() / "_mcp_scratch"
    try:
        resultado = developer_agent(state)

        print("6. Implementación generada:")
        print(json.dumps(resultado["implementation"], indent=2, ensure_ascii=False))

        impl = resultado["implementation"]
        checks = {
            "archivos_creados": impl.get("archivos_creados"),
            "pasos_seguidos": impl.get("pasos_seguidos"),
        }
        for campo, valor in checks.items():
            flag = "OK" if valor else "ALERTA - vacío"
            print(f"   {flag} - {campo}: {len(valor) if valor else 0} elemento(s)")
    finally:
        # En un try/finally para que el scratch quede limpio incluso si el
        # paso de impresión falla (ej. consola Windows sin UTF-8) — de lo
        # contrario un archivo del _mcp_scratch anterior queda "ya existe"
        # para la siguiente corrida, confundiendo al LLM en el próximo intento.
        print("7. Limpiando el scratch de la prueba (_mcp_scratch/)...")
        if scratch_dir.is_dir():
            import shutil

            shutil.rmtree(scratch_dir, ignore_errors=True)
            print("   OK - _mcp_scratch/ eliminado.")

    print("8. Forzando flush de traces a Langfuse...")
    flush_traces()

    print("\nListo. Siguiente paso: agents/testing_agent.py (Fase 6, agente 5/6).")
