"""Pruebas del protocolo MCP completo contra nuestro servidor (Fase 5, Paso 3).

Levanta mcp_server.server como subproceso stdio (exactamente como lo hará
cualquier cliente MCP, incluidos los agentes en Fase 6) y ejercita el ciclo
initialize -> list_tools -> call_tool.

Ejecución:
    .venv/Scripts/python.exe -m pytest tests/test_mcp_protocol.py -v
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_server.server import ROOT as CLONE_ROOT

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = ["create_file", "list_files", "read_file", "search_code", "update_file"]


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=str(REPO_ROOT),
    )


def _text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def _run(coro):
    return asyncio.run(coro)


def test_protocolo_inicializa_y_lista_las_tools():
    async def flow():
        async with stdio_client(_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                return sorted(t.name for t in listed.tools)

    assert _run(flow()) == EXPECTED_TOOLS


def test_protocolo_lee_archivo_real_por_call_tool():
    async def flow():
        async with stdio_client(_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "read_file", {"file_path": "README.md"}
                )
                assert not result.is_error
                return _text_of(result)

    text = _run(flow())
    assert "Solicitud" in text


def test_protocolo_busca_codigo_por_call_tool():
    async def flow():
        async with stdio_client(_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "list_files", {"subpath": "Solicitud_de_Vacaiones/Controllers"}
                )
                assert not result.is_error
                return _text_of(result)

    listing = _run(flow())
    assert "HomeController.cs" in listing


def test_protocolo_propaga_error_de_sandbox_como_tool_error():
    async def flow():
        async with stdio_client(_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "read_file", {"file_path": "../.env"}
                )
                return result.is_error

    assert _run(flow()) is True


def test_protocolo_crea_md_de_cambios_y_lo_actualiza():
    rel = "_mcp_scratch/protocolo_cambios.md"

    async def flow():
        async with stdio_client(_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                r1 = await session.call_tool(
                    "create_file",
                    {
                        "file_path": rel,
                        "content": "# Cambios implementados\n\n- via protocolo MCP",
                    },
                )
                assert not r1.is_error
                r2 = await session.call_tool(
                    "update_file",
                    {
                        "file_path": rel,
                        "old_text": "- via protocolo MCP",
                        "new_text": "- escritura validada end-to-end por el protocolo",
                    },
                )
                assert not r2.is_error
                return _text_of(r2)

    try:
        msg = _run(flow())
        assert "actualizado" in msg
        creado = CLONE_ROOT / "_mcp_scratch" / "protocolo_cambios.md"
        assert creado.is_file()
        assert "end-to-end" in creado.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(CLONE_ROOT / "_mcp_scratch", ignore_errors=True)


def test_protocolo_create_file_rechaza_existente_como_tool_error():
    rel = "_mcp_scratch/duplicado_protocolo.txt"

    async def flow():
        async with stdio_client(_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool(
                    "create_file", {"file_path": rel, "content": "v1"}
                )
                result = await session.call_tool(
                    "create_file", {"file_path": rel, "content": "v2"}
                )
                return result.is_error

    try:
        assert _run(flow()) is True
    finally:
        shutil.rmtree(CLONE_ROOT / "_mcp_scratch", ignore_errors=True)
