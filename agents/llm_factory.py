"""Factory compartida del cliente LLM (llega con el tercer agente).

Qué hace:
    Centraliza la construcción del cliente ChatOpenAI apuntando a OpenRouter,
    para que los agentes lo importen en vez de duplicar la misma función.

Responsabilidad dentro del sistema:
    Único punto de configuración del proveedor LLM (modelo, base_url,
    api_key) usado por product_agent, architect_agent, security_agent y los
    que sigan.

Decisiones:
    - Se extrae recién ahora, con security_agent.py (agente 3/6): tanto
      product_agent.py como architect_agent.py dejaban anotado en su
      docstring que duplicar _build_llm() era aceptable solo hasta que un
      tercer agente lo necesitara también — construir esto antes habría sido
      infraestructura especulativa sin un tercer caso de uso real.
    - build_llm(temperature=0) mantiene el mismo default que ya usaban los
      dos agentes anteriores (salida determinista para documentos técnicos),
      pero queda parametrizable por si un agente futuro necesita más
      variabilidad (ej. redacción de mensajes al usuario).
"""

import os

from langchain_openai import ChatOpenAI

__all__ = ["build_llm"]


def build_llm(temperature: float = 0) -> ChatOpenAI:
    """Construye el cliente LLM apuntando a OpenRouter vía variables de entorno."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY no está configurada. Copia .env.example a .env "
            "y completa OPENROUTER_API_KEY antes de correr este agente."
        )
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL_NAME", "nvidia/nemotron-3.5-lightning:free"),
        base_url=os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=api_key,
        temperature=temperature,
    )
