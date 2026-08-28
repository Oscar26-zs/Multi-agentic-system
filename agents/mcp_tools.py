"""Utilidades compartidas para hablar con el servidor MCP propio (llega con el
segundo agente que necesita un ciclo de tool-calling contra mcp_server/server.py).

Qué hace:
    Centraliza la plomería de conexión (lanzar el servidor como subproceso
    stdio, convertir sus tools al formato que espera `bind_tools()`, leer el
    resultado de una llamada) y el cálculo determinista de qué archivo se
    creó/modificó y su diff — para que cada agente que ejecuta un ciclo ReAct
    contra el MCP no la reimplemente.

Responsabilidad dentro del sistema:
    Punto único de integración MCP-cliente para agents/*.py. mcp_server/server.py
    sigue siendo la única fuente de verdad de qué tools existen y qué hacen;
    este módulo solo sabe conversar con esa API por protocolo.

Decisiones:
    - Se extrae con testing_agent.py (agente 5/6): developer_agent.py (agente
      4/6) fue el primero en necesitar este ciclo, y en su momento se dejó la
      plomería como funciones privadas de ese archivo porque todavía era el
      único caso de uso — mismo criterio que ya se aplicó con
      agents/llm_factory.py. Con un segundo agente que necesita el mismo
      patrón (conectar, listar tools, ejecutar, trackear create_file/
      update_file), duplicarlo ya no se justifica.
    - track_file_change() NO decide qué hacer con el resultado (no acumula en
      un set ni en un dict): devuelve una tupla y deja que cada agente decida
      cómo guardarla, porque developer_agent.py necesita el diff completo y
      testing_agent.py solo necesita saber qué archivo se tocó.
    - invoke_with_retry() nace tras un 429 real de Groq en producción
      ("Rate limit reached... tokens per minute"): a diferencia de
      agents/llm_factory.py (build_llm/invoke_structured), el LLM del ciclo
      ReAct se bindea con bind_tools() UNA sola vez al principio del ciclo
      (ver decisión en agents/developer_agent.py: cambiar de proveedor a
      mitad de conversación rompería el formato de tool calls ya
      establecido) — así que no tenía NINGÚN reintento propio, y un 429 crudo
      del proveedor reventaba sin manejo dentro del contexto async que
      mantiene la sesión MCP abierta (visto como "unhandled errors in a
      TaskGroup"). Esta función reintenta con espera ante 429 SOBRE EL MISMO
      cliente (nunca cambia de proveedor a mitad de conversación); para
      cualquier otro error, no reintenta — no tiene sentido esperar por algo
      que no se va a arreglar solo.
    - FileChange / run_exploration_loop() / apply_file_changes() nacen de
      separar "planificar" de "ejecutar" en developer_agent.py y
      testing_agent.py: el ciclo ReAct original (explorar Y escribir en la
      misma conversación abierta) hacía crecer el historial en cada turno
      hasta romper el límite de tokens/minuto del proveedor (413), y dejaba
      la escritura real a merced de que el LLM no alucinara una tool a mitad
      de una conversación larga (pasó de verdad: "print_tree"). Ahora:
      run_exploration_loop() corre un ciclo ReAct CORTO bindeado SOLO con
      tools de lectura (nunca con create_file/update_file/run_tests, así la
      escritura queda estructuralmente imposible en esta fase) para juntar
      contexto; con eso, el agente pide UNA sola salida estructurada (un
      plan compacto, no más conversación abierta); apply_file_changes()
      ejecuta ese plan iterando en Python, sin ningún LLM de por medio — la
      parte mecánica deja de costar tokens y de poder alucinar una tool.
"""

import asyncio
import difflib
import json
import sys
import time
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from mcp import StdioServerParameters
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent

__all__ = [
    "REPO_ROOT",
    "mcp_server_params",
    "tool_to_openai_schema",
    "result_text",
    "summarize_args",
    "unified_diff",
    "track_file_change",
    "invoke_with_retry",
    "FileChange",
    "run_exploration_loop",
    "summarize_exploration",
    "apply_file_changes",
]


def mcp_server_params() -> StdioServerParameters:
    """Parámetros para lanzar mcp_server.server como subproceso stdio.

    cwd=REPO_ROOT (raíz del workspace, NO el repo objetivo): el propio
    servidor resuelve REPO_TARGET_PATH desde su .env al arrancar, igual que
    en tests/test_mcp_protocol.py.
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=str(REPO_ROOT),
    )


def tool_to_openai_schema(tool) -> dict:
    """Convierte una mcp.types.Tool al formato {"type": "function", ...} de bind_tools."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema,
        },
    }


def result_text(result) -> str:
    """Extrae el texto de un CallToolResult (concatena todos los bloques de texto)."""
    return "".join(getattr(chunk, "text", "") for chunk in getattr(result, "content", []))


def summarize_args(args: dict) -> str:
    """Representación corta de los argumentos de una tool call, para bitácora legible."""
    if "file_path" in args:
        return args["file_path"]
    if "subpath" in args:
        return args.get("subpath") or "(raíz)"
    if "query" in args:
        return repr(args["query"])
    return json.dumps(args, ensure_ascii=False)


def unified_diff(old_text: str, new_text: str, file_path: str) -> str:
    diff_lines = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
    )
    return "".join(diff_lines)


def invoke_with_retry(llm_with_tools, messages: list, max_intentos: int = 3, espera_base: float = 5.0):
    """Invoca el LLM (ya bindeado con bind_tools) reintentando ante 429 (rate
    limit) con espera creciente. NUNCA cambia de proveedor a mitad de
    conversación — solo reintenta con el mismo cliente. Cualquier error que
    no sea 429 se relanza de inmediato, sin reintentar.
    """
    for intento in range(1, max_intentos + 1):
        try:
            return llm_with_tools.invoke(messages)
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            if status_code != 429 or intento == max_intentos:
                raise
            espera = espera_base * intento
            print(
                f"      REINTENTANDO tras rate limit (429), esperando {espera:.0f}s "
                f"(intento {intento + 1}/{max_intentos})..."
            )
            time.sleep(espera)


def track_file_change(tool_call: dict, text: str, is_error: bool) -> tuple[str, str, str] | None:
    """Si tool_call fue un create_file/update_file exitoso, devuelve (file_path, bucket, diff).

    bucket es "creado" o "modificado" (create_file cuenta como "modificado" si
    el mensaje del servidor dice "reemplazado" — ver mcp_server/server.py).
    Devuelve None para tools de lectura, para run_tests, o para llamadas que
    fallaron (is_error=True): no hay archivo que trackear en esos casos.
    """
    if is_error:
        return None
    name = tool_call["name"]
    args = tool_call.get("args", {})

    if name == "create_file":
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        bucket = "modificado" if "reemplazado" in text else "creado"
        return file_path, bucket, unified_diff("", content, file_path)
    if name == "update_file":
        file_path = args.get("file_path", "")
        diff = unified_diff(args.get("old_text", ""), args.get("new_text", ""), file_path)
        return file_path, "modificado", diff
    return None


class FileChange(BaseModel):
    """Un cambio de archivo propuesto por un plan estructurado (developer_agent.py
    o testing_agent.py) — se ejecuta luego con apply_file_changes(), sin LLM."""

    file_path: str = Field(..., description="Ruta del archivo, relativa a la raíz del repo objetivo.")
    accion: Literal["crear", "editar"] = Field(
        ..., description="'crear' para un archivo nuevo, 'editar' para uno existente."
    )
    contenido: str = Field(
        default="", description="Contenido completo del archivo nuevo; obligatorio si accion='crear'."
    )
    old_text: str = Field(
        default="",
        description="Fragmento EXACTO (mismos espacios/indentación) a reemplazar; obligatorio si accion='editar'.",
    )
    new_text: str = Field(default="", description="Texto de reemplazo; obligatorio si accion='editar'.")
    razon: str = Field(..., description="Por qué hace falta este cambio puntual (trazabilidad).")


async def run_exploration_loop(
    session,
    llm,
    system_prompt: str,
    human_message: str,
    max_turnos: int,
    nombre_agente: str,
    tool_result_char_limit: int = 1500,
) -> list:
    """Ciclo ReAct CORTO bindeado SOLO con tools de lectura (list_files,
    read_file, search_code) — nunca con create_file/update_file/run_tests, así
    la escritura queda estructuralmente imposible en esta fase. Junta el
    contexto mínimo necesario para que el caller pida después un plan
    estructurado (invoke_structured) con ese contexto.

    Devuelve la lista de `messages` acumulada (System/Human/AI/Tool), lista
    para pasarla como contexto de la llamada que pide el plan final.
    """
    tools_result = await session.list_tools()
    tools_lectura = [
        tool_to_openai_schema(t)
        for t in tools_result.tools
        if t.name in ("list_files", "read_file", "search_code")
    ]
    llm_with_tools = llm.bind_tools(tools_lectura)
    messages: list = [SystemMessage(content=system_prompt), HumanMessage(content=human_message)]

    for turno in range(1, max_turnos + 1):
        print(f"      [{nombre_agente}] exploración {turno}/{max_turnos}: consultando al LLM...")
        try:
            ai_message = await asyncio.to_thread(invoke_with_retry, llm_with_tools, messages)
        except Exception as error:
            # Mismo criterio que el resto del sistema: un error del LLM acá no
            # debe tumbar nada — se corta la exploración con lo que se alcanzó
            # a juntar, y el caller igual pide el plan con ese contexto parcial.
            print(f"      [{nombre_agente}] ALERTA - error irrecuperable del LLM explorando, cortando: {error}")
            break
        messages.append(ai_message)
        if not ai_message.tool_calls:
            print(f"      [{nombre_agente}] el modelo no pidió más exploración; listo para el plan.")
            break
        for tool_call in ai_message.tool_calls:
            args_resumen = summarize_args(tool_call.get("args", {}))
            print(f"      [{nombre_agente}] -> {tool_call['name']}({args_resumen})")
            result = await session.call_tool(tool_call["name"], tool_call.get("args", {}))
            text = result_text(result)
            is_error = bool(getattr(result, "is_error", False))
            print(f"      [{nombre_agente}]    {'ERROR' if is_error else 'OK'}")
            messages.append(
                ToolMessage(content=text[:tool_result_char_limit], tool_call_id=tool_call["id"])
            )

    return messages


def summarize_exploration(messages: list) -> str:
    """Convierte la lista de messages de run_exploration_loop() (System/Human/
    AI-con-tool_calls/Tool) en un resumen de texto plano, SIN ningún mensaje
    de "el agente llamó una tool" a la vista.

    Nace de un problema real reproducido dos veces en producción: al pedirle
    el plan estructurado final reenviando la conversación cruda de
    exploración (con los AIMessage que tienen tool_calls), el modelo
    "reincidía" e intentaba volver a llamar list_files/read_file en esa
    respuesta — aunque el prompt le dijera explícitamente que ya no había
    tools disponibles. No es un problema de instrucciones poco claras: el
    modelo ve su propio turno anterior llamando una tool y repite el patrón
    por inercia. La solución es no reenviar esa conversación cruda: se arma
    un resumen de texto plano de los RESULTADOS (sin los objetos AIMessage
    con tool_calls) y se le pide el plan en un contexto nuevo de 2 mensajes,
    donde no hay ningún turno previo de tool-calling que el modelo pueda
    "continuar" reflexivamente.
    """
    partes: list[str] = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            partes.append(f"Resultado: {msg.content}")
        elif getattr(msg, "tool_calls", None):
            nombres = ", ".join(tc["name"] for tc in msg.tool_calls)
            partes.append(f"[se consultó: {nombres}]")
    return "\n\n".join(partes) if partes else "(no se exploró nada — sin llamadas a tools)"


async def apply_file_changes(
    session, cambios: list[FileChange], nombre_agente: str
) -> tuple[list[str], list[str], dict, list[str]]:
    """Ejecuta una lista de FileChange contra el servidor MCP real — SIN LLM,
    puro Python iterando la lista. Devuelve (archivos_creados,
    archivos_modificados, diffs, pasos), mismo shape que ya devolvía el ciclo
    ReAct anterior, para no tener que tocar el resto de cada agente.
    """
    archivos_creados: list[str] = []
    archivos_modificados: list[str] = []
    diffs: dict = {}
    pasos: list[str] = []

    for cambio in cambios:
        print(f"      [{nombre_agente}] -> {cambio.accion}({cambio.file_path})")
        if cambio.accion == "crear":
            result = await session.call_tool(
                "create_file", {"file_path": cambio.file_path, "content": cambio.contenido}
            )
        else:
            result = await session.call_tool(
                "update_file",
                {
                    "file_path": cambio.file_path,
                    "old_text": cambio.old_text,
                    "new_text": cambio.new_text,
                },
            )
        text = result_text(result)
        is_error = bool(getattr(result, "is_error", False))
        estado = "ERROR" if is_error else "OK"
        print(f"      [{nombre_agente}]    {estado}")
        pasos.append(f"{cambio.accion}({cambio.file_path}) -> {estado}: {cambio.razon}")
        if is_error:
            continue
        if cambio.accion == "crear":
            archivos_creados.append(cambio.file_path)
            diffs[cambio.file_path] = unified_diff("", cambio.contenido, cambio.file_path)
        else:
            archivos_modificados.append(cambio.file_path)
            diffs[cambio.file_path] = unified_diff(cambio.old_text, cambio.new_text, cambio.file_path)

    return archivos_creados, archivos_modificados, diffs, pasos
