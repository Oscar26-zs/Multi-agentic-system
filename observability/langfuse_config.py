"""Configuración de Langfuse.

Qué hace:
    Centraliza la inicialización del cliente de Langfuse y el trazado de la
    ejecución del sistema multiagente.

Responsabilidad dentro del sistema:
    Da visibilidad end-to-end: qué prompt usó cada agente, tokens consumidos,
    latencia por nodo y errores ocurridos, tanto en desarrollo como en
    producción.

Se espera que contenga cuando se implemente:
    - Inicialización del cliente con credenciales desde variables de entorno.
    - Callback/handler de tracing integrable con LangChain/LangGraph.
    - Spans por agente (metadata: rol, iteración actual, veredicto).
    - Registro de costos (tokens), latencias y eventos de error.
"""
