"""Agente de Producto (product_agent).

Qué hace:
    Interpreta el requerimiento inicial del usuario y genera una especificación
    estructurada del problema a resolver.

Responsabilidad dentro del sistema:
    Primer eslabón del pipeline: convierte lenguaje natural en una definición
    formal que los demás agentes consumen desde el estado compartido
    (EngineeringState).

Se espera que contenga cuando se implemente:
    - Prompt de sistema con el rol de analista de producto.
    - Invocación al LLM con structured output para producir:
        * actores involucrados,
        * reglas de negocio,
        * criterios de aceptación,
        * riesgos identificados.
    - Validación de la especificación generada y manejo de requerimientos
      ambiguos o incompletos.
"""
