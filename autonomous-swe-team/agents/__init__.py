"""Paquete de agentes del equipo de desarrollo autónomo.

Responsabilidad dentro del sistema:
    Agrupa a los seis agentes especializados que participan en el pipeline
    (producto, arquitectura, desarrollo, seguridad, testing y revisión).

Se espera que contenga cuando se implemente:
    - Las clases o factories de cada agente: ProductAgent, ArchitectAgent,
      DeveloperAgent, SecurityAgent, TestingAgent y ReviewerAgent.
    - Posibles utilidades compartidas entre agentes (configuración de LLM,
      bindings de tools MCP, handlers de tracing).
"""
