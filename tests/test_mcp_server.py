"""Pruebas directas de las tools del servidor MCP (Fase 5, Paso 2).

Ejecutan las funciones SIN protocolo MCP para aislar errores de lógica
propia de los errores de transporte. Verifican comportamiento contra los
archivos REALES del clon MVC en _sandbox/Solicitud_de_Vacaciones.

Ejecución:
    .venv/Scripts/python.exe -m pytest tests/test_mcp_server.py -v
"""

import os
import shutil
from pathlib import Path

import pytest

from mcp_server import server

CONTROLLER = "Solicitud_de_Vacaiones/Controllers/HomeController.cs"

EXPECTED_TOOLS = [
    "create_file",
    "list_files",
    "read_file",
    "run_tests",
    "search_code",
    "update_file",
]


def test_registra_las_tools_de_lectura_y_escritura():
    tools = sorted(t.name for t in __import__("asyncio").run(server.server.list_tools()))
    assert tools == EXPECTED_TOOLS


def test_list_files_devuelve_archivos_reales_del_mvc():
    files = server.list_files()
    assert len(files) > 50
    assert CONTROLLER in files
    assert "README.md" in files


def test_list_files_excluye_carpetas_de_ruido():
    files = server.list_files()
    ruidosas = [
        f
        for f in files
        if f.startswith(".git/")
        or "/bin/" in f
        or "/obj/" in f
        or f.startswith(".vscode/")
    ]
    assert ruidosas == []


def test_list_files_con_subpath_y_pattern():
    controllers = server.list_files(
        "Solicitud_de_Vacaiones/Controllers", pattern="*.cs"
    )
    assert controllers == [CONTROLLER]


def test_read_file_lee_cs_utf8():
    content = server.read_file(CONTROLLER)
    assert "HomeController" in content
    assert "Controller" in content


def test_read_file_soporta_utf16_con_bom():
    content = server.read_file("README.md")
    assert content.lstrip("\ufeff").startswith("# Solicitud")


def test_read_file_archivo_inexistente_lanza_error_claro():
    with pytest.raises(FileNotFoundError, match="no existe"):
        server.read_file("carpeta_inventada/archivo.cs")


def test_read_file_rechaza_escape_relativo():
    with pytest.raises(ValueError, match="escapa del repositorio"):
        server.read_file("../.env")


def test_read_file_rechaza_ruta_absoluta_fuera_del_repo():
    fuera = os.path.abspath(os.path.join(os.getcwd(), ".env"))
    with pytest.raises(ValueError, match="escapa del repositorio"):
        server.read_file(fuera)


def test_search_code_encuentra_el_controller():
    hits = server.search_code(r"class\s+\w*Controller", glob="*.cs")
    assert hits, "deberia encontrar HomeController"
    assert hits[0]["file"] == CONTROLLER
    assert hits[0]["line"] >= 1
    assert "class" in hits[0]["text"]


def test_search_code_regex_invalida_se_trata_como_literal():
    hits = server.search_code("(", glob="*.cs")
    assert all("(" in h["text"] for h in hits)


def test_search_code_respeta_max_results():
    hits = server.search_code("a", glob="*.cs", max_results=2)
    assert len(hits) <= 2


def test_safe_resolve_acepta_rutas_internas():
    assert server._safe_resolve("docs").is_dir()


# ---------- Tools de escritura (create_file / update_file) ----------


@pytest.fixture()
def scratch():
    """Carpeta temporal DENTRO del sandbox: permite probar escrituras sin tocar el MVC real."""
    d = server.ROOT / "_mcp_scratch"
    d.mkdir(exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_create_file_crea_archivo_nuevo(scratch):
    rel = "_mcp_scratch/cambios_implementados.md"
    msg = server.create_file(rel, "# Cambios implementados\n\n- item 1")
    assert "creado" in msg
    assert (
        scratch / "cambios_implementados.md"
    ).read_text(encoding="utf-8") == "# Cambios implementados\n\n- item 1"


def test_create_file_crea_carpetas_padre(scratch):
    rel = "_mcp_scratch/docs/profundo/nota.md"
    server.create_file(rel, "hola")
    assert (scratch / "docs/profundo/nota.md").is_file()


def test_create_file_rechaza_existente_sin_overwrite(scratch):
    rel = "_mcp_scratch/duplicado.txt"
    server.create_file(rel, "v1")
    with pytest.raises(ValueError, match="ya existe"):
        server.create_file(rel, "v2")
    assert (scratch / "duplicado.txt").read_text(encoding="utf-8") == "v1"


def test_create_file_con_overwrite_reemplaza_completo(scratch):
    rel = "_mcp_scratch/reemplazable.txt"
    server.create_file(rel, "v1")
    msg = server.create_file(rel, "v2", overwrite=True)
    assert "reemplazado" in msg
    assert (scratch / "reemplazable.txt").read_text(encoding="utf-8") == "v2"


def test_create_file_rechaza_escape_del_sandbox():
    with pytest.raises(ValueError, match="escapa del repositorio"):
        server.create_file("../fuera_del_repo.md", "contenido")


def test_update_file_reemplaza_fragmento_unico(scratch):
    f = scratch / "codigo.cs"
    f.write_text(
        "public class A {\n    public int X { get; set; }\n}", encoding="utf-8"
    )
    msg = server.update_file(
        "_mcp_scratch/codigo.cs",
        "public int X { get; set; }",
        "public int X { get; private set; }",
    )
    assert "actualizado" in msg
    content = f.read_text(encoding="utf-8")
    assert "private set" in content
    assert "{ get; set; }" not in content


def test_update_file_adapta_saltos_de_linea_nuevos_al_eol_del_archivo(scratch):
    f = scratch / "crlf.cs"
    f.write_bytes(b"a = 1;\r\nb = 2;\r\n")
    server.update_file("_mcp_scratch/crlf.cs", "b = 2;", "b = 2;\n// nota nueva")
    raw = f.read_bytes()
    assert raw.count(b"\r\n") == 3
    assert b"\n" not in raw.replace(b"\r\n", b"")  # sin LF sueltos


def test_update_file_preserva_utf16_con_bom(scratch):
    f = scratch / "utf16.md"
    f.write_bytes("# Titulo\r\n\r\nTexto original.".encode("utf-16"))
    server.update_file(
        "_mcp_scratch/utf16.md", "Texto original.", "Texto actualizado."
    )
    raw = f.read_bytes()
    assert raw.startswith(b"\xff\xfe")  # BOM UTF-16 LE preservado
    assert "Texto actualizado.".encode("utf-16-le") in raw


def test_update_file_falla_si_no_encuentra_fragmento_y_no_toca_nada(scratch):
    f = scratch / "estable.txt"
    f.write_text("contenido estable", encoding="utf-8")
    with pytest.raises(ValueError, match="NO fue encontrado"):
        server.update_file("_mcp_scratch/estable.txt", "zzz", "yyy")
    assert f.read_text(encoding="utf-8") == "contenido estable"


def test_update_file_falla_si_fragmento_ambiguo(scratch):
    f = scratch / "ambiguo.txt"
    f.write_text("linea igual\nlinea igual", encoding="utf-8")
    with pytest.raises(ValueError, match="2 veces"):
        server.update_file("_mcp_scratch/ambiguo.txt", "linea igual", "otra")


def test_tools_escritura_rechazan_rutas_fuera_del_repo():
    with pytest.raises(ValueError, match="escapa del repositorio"):
        server.update_file("../.env", "x", "y")


# ---------- Tool de ejecución (run_tests) ----------


def test_run_tests_subpath_inexistente_lanza_error_claro():
    with pytest.raises(FileNotFoundError, match="no existe"):
        server.run_tests("carpeta_inventada_para_tests")


def test_run_tests_rechaza_escape_del_sandbox():
    with pytest.raises(ValueError, match="escapa del repositorio"):
        server.run_tests("../fuera_del_repo")


def test_run_tests_parsea_lineas_de_resumen_de_dotnet_test():
    salida = (
        "Passed!  - Failed:     0, Passed:     3, Skipped:     0, Total:     3, Duration: 10 ms - A.dll\n"
        "Failed!  - Failed:     1, Passed:     2, Skipped:     1, Total:     4, Duration: 20 ms - B.dll\n"
    )
    matches = server._DOTNET_TEST_SUMMARY_RE.findall(salida)
    assert matches == [("0", "3", "0", "3"), ("1", "2", "1", "4")]
