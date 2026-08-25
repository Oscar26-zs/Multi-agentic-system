"""Servidor MCP sobre el clon del repo MVC (Fase 5 + escritura anticipada).

Qué hace:
    Implementa un servidor Model Context Protocol que expone tools para
    operar sobre el repositorio del sistema MVC del usuario:
        - Lectura:  list_files, read_file, search_code
        - Escritura: create_file (archivos nuevos, p. ej. documentacion de
          cambios en .md) y update_file (reemplazo quirurgico y unico de un
          fragmento de codigo existente).
    Todas las rutas se resuelven contra el clon local definido en
    REPO_TARGET_PATH (.env); ninguna tool puede escapar de esa raiz
    (sandboxing propio + proteccion nativa del SDK).

Responsabilidad dentro del sistema:
    Unica via por la que los agentes leen y modifican codigo real; aisla el
    acceso al filesystem detras de tools MCP auditables. run_tests y
    get_diff se agregaran despues (Fase 6) sobre esta misma base.

Detalles de robustez:
    - Encoding preservado: detecta UTF-16 con BOM (tipico de Visual Studio
      en este repo), UTF-8 con BOM y UTF-8 plano, y re-escribe en el MISMO
      formato para no corromper archivos.
    - Line endings: update_file adapta el texto nuevo al EOL dominante del
      archivo (CRLF vs LF) para evitar diffs gigantes.

Ejecucion standalone (transporte stdio):
    python -m mcp_server.server

Notas del SDK (mcp 2.0.0):
    - MCPServer es el sucesor de FastMCP: las tools se registran con el
      decorador @server.tool().
    - run_stdio_async() es coroutine → se lanza con asyncio.run(...).
"""

import asyncio
import fnmatch
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from mcp.server import MCPServer

load_dotenv()

# Raiz sandboxeada: unica zona del filesystem accesible por las tools.
ROOT = Path(os.environ["REPO_TARGET_PATH"]).resolve()

# Carpetas que no aportan a lectura de codigo (artefactos .NET, VCS, caches)
# mas el scratch usado por la suite de pruebas.
EXCLUDED_DIRS = {
    ".git",
    ".vs",
    ".vscode",
    ".github",
    "bin",
    "obj",
    "node_modules",
    "packages",
    "__pycache__",
    "_mcp_scratch",
}


def _safe_resolve(relative_path: str) -> Path:
    """Resuelve relative_path contra ROOT y garantiza que quede DENTRO de ROOT."""
    candidate = (ROOT / relative_path).resolve()
    if candidate != ROOT and ROOT not in candidate.parents:
        raise ValueError(
            f"Acceso denegado: '{relative_path}' escapa del repositorio permitido."
        )
    return candidate


def _iter_repo_files(base: Path):
    """Genera Paths absolutos bajo base, sin entrar a carpetas excluidas."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for filename in filenames:
            yield Path(dirpath) / filename


def _detect_encoding(data: bytes) -> str:
    """Deteccion por BOM: utf-16 (FF FE / FE FF), utf-8-sig (EF BB BF) o utf-8."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "utf-8"


def _dominant_eol(text: str) -> str:
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    return "\r\n" if crlf >= lf_only else "\n"


def _to_file_eol(text: str, eol: str) -> str:
    """Normaliza los saltos de text hacia el EOL objetivo del archivo."""
    return text.replace("\r\n", "\n").replace("\n", eol)


server = MCPServer(
    name="mvc-repo-tools",
    instructions=(
        "Tools sobre el repositorio del sistema MVC (ASP.NET Core) del "
        "usuario. Lectura: explora la estructura (list_files), lee "
        "contenido (read_file) y busca codigo (search_code). Escritura: "
        "crea archivos nuevos como documentacion de cambios (create_file) "
        "y modifica fragmentos exactos de codigo existente (update_file). "
        "Los cambios ocurren en la rama actual del clon local."
    ),
)


@server.tool()
def list_files(subpath: str = "", pattern: str = "*") -> list[str]:
    """Lista archivos del repositorio como rutas relativas a la raíz.

    Args:
        subpath: subcarpeta desde donde listar ("" = raíz del repo).
        pattern: patrón glob aplicado a la ruta relativa completa o al nombre.
    """
    base = _safe_resolve(subpath)
    if not base.is_dir():
        raise FileNotFoundError(f"No existe la carpeta '{subpath}' en el repositorio.")

    matches = []
    for full in _iter_repo_files(base):
        rel = full.relative_to(ROOT).as_posix()
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(full.name, pattern):
            matches.append(rel)
    return sorted(matches)


@server.tool()
def read_file(file_path: str, max_bytes: int = 524288) -> str:
    """Devuelve el contenido de texto de un archivo del repositorio.

    Args:
        file_path: ruta relativa a la raíz del repo.
        max_bytes: límite de tamaño (rechaza archivos más grandes).
    """
    path = _safe_resolve(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"El archivo '{file_path}' no existe en el repositorio.")

    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"'{file_path}' pesa {size} bytes y supera el límite de {max_bytes}."
        )

    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        # Null bytes: binario real O texto UTF-16 (tipico de Visual Studio).
        try:
            return data.decode("utf-16")  # respeta el BOM (FF FE / FE FF) si existe
        except UnicodeDecodeError:
            raise ValueError(
                f"'{file_path}' parece binario; solo se leen archivos de texto."
            )
    return data.decode("utf-8", errors="replace")


@server.tool()
def search_code(query: str, glob: str = "*.cs", max_results: int = 50) -> list[dict]:
    """Busca query (regex o texto literal) línea por línea en archivos que cumplen glob.

    Args:
        query: expresión regular; si es inválida se busca como texto literal.
        glob: patrón de nombre de archivo (ej. "*.cs", "*.cshtml").
        max_results: máximo de coincidencias a devolver.

    Devuelve objetos {"file", "line", "text"} con ruta relativa, número de
    línea y fragmento recortado.
    """
    try:
        rx = re.compile(query)
    except re.error:
        rx = re.compile(re.escape(query))

    results: list[dict] = []
    for full in _iter_repo_files(ROOT):
        if not fnmatch.fnmatch(full.name, glob):
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                results.append(
                    {
                        "file": full.relative_to(ROOT).as_posix(),
                        "line": line_number,
                        "text": line.strip()[:200],
                    }
                )
                if len(results) >= max_results:
                    return results
    return results


@server.tool()
def create_file(file_path: str, content: str, overwrite: bool = False) -> str:
    """Crea un archivo nuevo dentro del repositorio (siempre UTF-8).

    Args:
        file_path: ruta relativa donde crear el archivo (crea carpetas padre).
        content: contenido completo del archivo.
        overwrite: si es False (default) falla cuando el archivo ya existe;
            con True lo reemplaza por completo.
    """
    path = _safe_resolve(file_path)
    if path.is_dir():
        raise ValueError(f"'{file_path}' es una carpeta existente, no un archivo.")
    existed = path.exists()
    if existed and not overwrite:
        raise ValueError(
            f"'{file_path}' ya existe. Usa overwrite=True para reemplazarlo "
            "completo o update_file para editar solo un fragmento."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    size = len(content.encode("utf-8"))
    path.write_bytes(content.encode("utf-8"))
    verbo = "reemplazado" if existed else "creado"
    return f"OK: {file_path} {verbo} ({size} bytes)."


@server.tool()
def update_file(file_path: str, old_text: str, new_text: str) -> str:
    """Reemplaza UNA aparicion exacta y unica de old_text por new_text.

    Edicion quirurgica para agentes: si old_text no existe o aparece varias
    veces, falla sin tocar nada, pidiendo mas contexto. Preserva el encoding
    del archivo original (incluido UTF-16 con BOM) y adapta los saltos de
    linea del texto nuevo al EOL dominante del archivo.

    Args:
        file_path: ruta relativa del archivo a editar.
        old_text: fragmento exacto a reemplazar (unico en el archivo).
        new_text: reemplazo.
    """
    path = _safe_resolve(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"El archivo '{file_path}' no existe en el repositorio.")

    data = path.read_bytes()
    encoding = _detect_encoding(data)
    text = data.decode(encoding)

    eol = _dominant_eol(text)
    target_old = _to_file_eol(old_text, eol)
    count = text.count(target_old)

    if count == 0:
        raise ValueError(
            f"old_text NO fue encontrado en '{file_path}'. Copia el fragmento "
            "exacto desde read_file (respetando espacios e indentacion) y "
            "vuelve a intentar."
        )
    if count > 1:
        raise ValueError(
            f"old_text aparece {count} veces en '{file_path}'; agrega mas "
            "contexto alrededor para que el fragmento sea unico."
        )

    adapted_new = _to_file_eol(new_text, eol)
    updated = text.replace(target_old, adapted_new, 1)
    path.write_bytes(updated.encode(encoding))
    return (
        f"OK: {file_path} actualizado "
        f"(+{len(adapted_new)} chars, -{len(target_old)} chars)."
    )


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
