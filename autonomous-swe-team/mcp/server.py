"""Servidor MCP propio del proyecto.

Qué hace:
    Implementa un servidor Model Context Protocol que expone tools para operar
    sobre el repositorio de ejemplo de forma controlada y auditable.

Responsabilidad dentro del sistema:
    Única vía por la que los agentes tocan código real; aísla el acceso al
    filesystem y a la ejecución de pruebas detrás de tools MCP.

Se espera que contenga cuando se implemente:
    Tools mínimas:
        - read_file: leer el contenido de un archivo.
        - list_files: listar archivos/directorios del repo.
        - search_code: buscar patrones/texto en el código.
        - create_file: crear o sobrescribir archivos.
        - get_diff: obtener el diff de los cambios realizados.
        - run_tests: ejecutar la suite de pruebas del repo de ejemplo.
    - Validación/sandboxing de rutas para impedir accesos fuera del repo.
    - Arranque standalone (stdio/SSE) para conectarlo desde los agentes.
"""
