"""Agente Desarrollador (developer_agent).

Qué hace:
    Propone y genera la implementación de la solución siguiendo la propuesta
    técnica del Architect Agent y la especificación del Product Agent.

Responsabilidad dentro del sistema:
    Único agente autorizado a modificar código del repositorio objetivo; todas
    sus operaciones sobre archivos se realizan mediante las tools MCP definidas
    en mcp/server.py (read_file, list_files, search_code, create_file, get_diff).

Se espera que contenga cuando se implemente:
    - Prompt de sistema con el rol de ingeniero de software senior.
    - Conexión al servidor MCP propio para leer/modificar el repo de ejemplo.
    - Lógica ReAct/tool-calling para explorar el repositorio y crear/editar
      archivos de forma controlada.
    - Consulta al retriever RAG de desarrollo (coding-standards,
      clean-code-guidelines) antes de escribir código.
    - Registro del diff generado en el estado compartido.
"""
