"""Factory compartida del cliente LLM (llega con el tercer agente).

Qué hace:
    Centraliza la construcción del cliente ChatOpenAI, probando varios
    proveedores gratuitos EN ORDEN (con un ping barato) hasta encontrar uno
    que responda, para que los agentes no dependan de la cuota compartida de
    un solo proveedor gratuito.

Responsabilidad dentro del sistema:
    Único punto de configuración del proveedor LLM (modelo, base_url,
    api_key) usado por product_agent, architect_agent, security_agent,
    developer_agent, testing_agent y reviewer_agent.

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
    - Fallback multi-proveedor (NVIDIA NIM -> Groq -> Google AI Studio ->
      OpenRouter): los modelos gratuitos tienen cuotas compartidas
      impredecibles; si uno falla (rate limit, modelo caído, key inválida),
      build_llm() prueba el siguiente en vez de tumbar el agente. NVIDIA va
      primero porque hostea directamente el modelo (nemotron) para el que ya
      están afinados los prompts (method="function_calling" en cada agente);
      OpenRouter queda último como red de seguridad porque es el proveedor
      original, ya confirmado que funciona.
    - La selección se hace UNA SOLA VEZ por llamada a build_llm(), no por
      cada turno de una conversación: developer_agent.py y testing_agent.py
      hacen bind_tools() sobre el resultado y lo reusan durante todo su ciclo
      ReAct — cambiar de proveedor a mitad de esa conversación rompería el
      formato de tool calls. Cada función de agente llama a build_llm() al
      principio, así que el ping extra por llamada es aceptable.
    - Cada proveedor solo se intenta si su `<PROVEEDOR>_API_KEY` está
      presente en el entorno; si ninguna lo está, el error final indica
      exactamente qué variables faltan.
    - invoke_structured() nace tras un fallo real en producción: security_agent
      pasó el ping de build_llm() contra NVIDIA (respuesta de texto simple),
      pero al pedirle SecurityReview (lista de objetos anidados) el modelo no
      logró generar function-calling válido y with_structured_output()
      devolvió None en silencio — el ping no detecta esta falla porque no usa
      schema. invoke_structured() hace el fallback AL NIVEL DE LA LLAMADA REAL
      (mismo orden de proveedores): si un proveedor devuelve None o lanza
      excepción al generar el schema, prueba el siguiente antes de rendirse.
      Los agentes con salida estructurada simple (product_agent,
      architect_agent, security_agent, reviewer_agent, y el resumen final de
      developer_agent/testing_agent) deben usar esta función en vez de
      build_llm().with_structured_output(...).invoke(...) directo.
"""

import os

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

__all__ = ["build_llm", "invoke_structured"]

_PROVEEDORES = [
    {
        "nombre": "NVIDIA NIM",
        "api_key_env": "NVIDIA_API_KEY",
        "base_url": os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "model": os.getenv("NVIDIA_MODEL_NAME", "nvidia/nemotron-3.5-lightning-30b-a3b"),
    },
    {
        "nombre": "Groq",
        "api_key_env": "GROQ_API_KEY",
        "base_url": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        "model": os.getenv("GROQ_MODEL_NAME", "openai/gpt-oss-120b"),
    },
    {
        "nombre": "Google AI Studio",
        "api_key_env": "GOOGLE_API_KEY",
        "base_url": os.getenv(
            "GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
        ),
        "model": os.getenv("GOOGLE_MODEL_NAME", "gemini-2.0-flash"),
    },
    {
        "nombre": "OpenRouter",
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url": os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        "model": os.getenv("LLM_MODEL_NAME", "nvidia/nemotron-3.5-lightning:free"),
    },
]


def _probar_proveedor(proveedor: dict, temperature: float) -> ChatOpenAI | None:
    """Construye el cliente de un proveedor y lo valida con un ping barato.

    Devuelve None (sin lanzar excepción) si la key falta o el proveedor no
    responde, para que build_llm() pueda seguir con el siguiente de la lista.
    """
    api_key = os.getenv(proveedor["api_key_env"])
    if not api_key:
        return None

    llm = ChatOpenAI(
        model=proveedor["model"],
        base_url=proveedor["base_url"],
        api_key=api_key,
        temperature=temperature,
    )
    try:
        llm.invoke([HumanMessage(content="ping")])
    except Exception as error:
        print(
            f"   ALERTA - {proveedor['nombre']} no respondió ({error}); "
            "probando el siguiente proveedor..."
        )
        return None

    print(f"   OK - usando {proveedor['nombre']} (modelo: {proveedor['model']}).")
    return llm


def build_llm(temperature: float = 0) -> ChatOpenAI:
    """Construye el cliente LLM probando proveedores en orden hasta que uno responda.

    Orden: NVIDIA NIM -> Groq -> Google AI Studio -> OpenRouter.
    """
    for proveedor in _PROVEEDORES:
        llm = _probar_proveedor(proveedor, temperature)
        if llm is not None:
            return llm

    faltantes = ", ".join(p["api_key_env"] for p in _PROVEEDORES)
    raise ValueError(
        "Ningún proveedor LLM respondió. Configura al menos una de estas "
        f"variables en tu .env: {faltantes}."
    )


def invoke_structured(schema, messages: list, method: str = "function_calling", temperature: float = 0):
    """Genera salida estructurada probando proveedores en orden hasta obtener una respuesta válida.

    A diferencia de build_llm(), la "prueba" de cada proveedor es la llamada
    real con with_structured_output(schema) — algunos proveedores responden a
    un ping simple pero fallan al generar function-calling para un schema con
    objetos anidados, devolviendo None en vez de lanzar una excepción. Ese
    None hay que detectarlo aquí, porque el .invoke() del caller lo daría por
    válido y tronaría más adelante con AttributeError al llamar .model_dump().

    Args:
        schema: clase Pydantic que define la forma esperada de la respuesta.
        messages: lista de mensajes (SystemMessage/HumanMessage/...) a enviar.
        method: modo de with_structured_output (default "function_calling",
            igual que el resto del código, más tolerante en modelos gratuitos).
        temperature: igual semántica que en build_llm().

    Devuelve una instancia de `schema`. Lanza ValueError si ningún proveedor
    configurado logró generar una respuesta válida.
    """
    errores: list[str] = []
    for proveedor in _PROVEEDORES:
        api_key = os.getenv(proveedor["api_key_env"])
        if not api_key:
            continue

        llm = ChatOpenAI(
            model=proveedor["model"],
            base_url=proveedor["base_url"],
            api_key=api_key,
            temperature=temperature,
        )
        try:
            resultado = llm.with_structured_output(schema, method=method).invoke(messages)
        except Exception as error:
            print(
                f"   ALERTA - {proveedor['nombre']} lanzó un error generando salida "
                f"estructurada ({error}); probando el siguiente proveedor..."
            )
            errores.append(f"{proveedor['nombre']}: {error}")
            continue

        if resultado is None:
            print(
                f"   ALERTA - {proveedor['nombre']} no devolvió salida estructurada válida "
                "(probablemente no logró generar function-calling para este schema); "
                "probando el siguiente proveedor..."
            )
            errores.append(f"{proveedor['nombre']}: devolvió None")
            continue

        print(f"   OK - usando {proveedor['nombre']} (modelo: {proveedor['model']}) para salida estructurada.")
        return resultado

    detalle = "; ".join(errores) if errores else "ninguna API key configurada"
    raise ValueError(f"Ningún proveedor LLM devolvió salida estructurada válida ({detalle}).")
