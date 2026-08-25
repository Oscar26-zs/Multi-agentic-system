"""Conftest raíz de pytest.

Su presencia en la raíz hace que pytest inserte esta carpeta en sys.path,
permitiendo que los tests importen los paquetes del proyecto directamente
(mcp_server, graph, observability, rag) sin instalaciones ni hacks.
"""
