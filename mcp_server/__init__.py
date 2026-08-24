"""Paquete del servidor MCP propio del proyecto.

Nota de nombrado: el paquete se llama `mcp_server` (y NO `mcp`) porque la
carpeta `mcp/` eclipsaría al SDK instalado `mcp` cuando se ejecuta desde la
raíz del workspace, rompiendo todos los imports del SDK.

Responsabilidad dentro del sistema:
    Contiene server.py: el servidor Model Context Protocol que expone tools
    sobre el clon local del repo MVC, usadas por los agentes. Hoy: 3 de
    lectura (list_files/read_file/search_code) + 2 de escritura
    (create_file/update_file). run_tests y get_diff llegan despues (Fase 6).
"""
