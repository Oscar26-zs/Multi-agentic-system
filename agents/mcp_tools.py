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
"""

import difflib
import json
import sys
import time
from pathlib import Path

from mcp import StdioServerParameters

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
