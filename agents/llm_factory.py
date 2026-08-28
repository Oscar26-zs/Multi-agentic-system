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
    - Fallback multi-proveedor (Groq -> NVIDIA NIM -> OpenRouter -> Google AI
      Studio): los modelos gratuitos tienen cuotas compartidas impredecibles;
      si uno falla (rate limit, modelo caído, key inválida, timeout),
      build_llm() prueba el siguiente en vez de tumbar el agente. Groq va
      primero por disponibilidad real observada: NVIDIA empezó primero
      (hostea directamente el modelo nemotron para el que están afinados los
      prompts), pero en la práctica quedó devolviendo 404 en cascada (caída
      del lado de NVIDIA) y arrastrando a OpenRouter con el mismo error —
      Groq fue el único proveedor que siguió respondiendo. Si esto se
      revierte, el orden es lo primero a ajustar.
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
    - Reintento corto (2 intentos) por proveedor antes de pasar al siguiente,
      SOLO si el error es reintentable: nace de un 504 real de NVIDIA en
      producción (gateway compartido gratuito + modelo de 30B + salida
      estructurada larga = más chance de timeout puntual). Un 504/5xx/error de
      red suele ser transitorio — abandonar el proveedor al primer fallo
      descarta de más un proveedor gratuito ya afinado en los prompts
      (method="function_calling") por una demora de un instante. Un 4xx (401
      key inválida, 404 modelo no existe, 400 request mal formada) NUNCA se
      reintenta: no se va a arreglar solo, y esperar ahí solo demora llegar al
      siguiente proveedor.
    - timeout=_REQUEST_TIMEOUT_SECONDS explícito en TODO ChatOpenAI(...): sin
      esto, el cliente usa el default del SDK de openai (varios minutos) y,
      peor, si el servidor deja la conexión abierta sin mandar más datos (no
      responde con error, simplemente no responde), el socket puede quedarse
      esperando de forma indefinida. Se observó en producción una corrida real
      donde un turno del ciclo ReAct de developer_agent.py tardó más de 1 HORA
      entre un intento y el siguiente — sin este timeout, ni el reintento ni
      el fallback a otro proveedor se disparan nunca, porque la excepción que
      los activa jamás llega. Con el timeout, el peor caso pasa de
      "potencialmente indefinido" a `_MAX_INTENTOS_POR_PROVEEDOR` x
      len(_PROVEEDORES) x _REQUEST_TIMEOUT_SECONDS como cota superior real.
"""

import os
import time

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

__all__ = ["build_llm", "invoke_structured"]

_MAX_INTENTOS_POR_PROVEEDOR = 2  # intento original + 1 reintento
_RETRY_DELAY_SECONDS = 2.0
_REQUEST_TIMEOUT_SECONDS = 600.0  # cota dura por intento (10 min): evita cuelgues indefinidos de socket

_PROVEEDORES = [
    {
        "nombre": "Groq",
        "api_key_env": "GROQ_API_KEY",
        "base_url": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        "model": os.getenv("GROQ_MODEL_NAME", "openai/gpt-oss-120b"),
    },
    {
        "nombre": "NVIDIA NIM",
        "api_key_env": "NVIDIA_API_KEY",
        "base_url": os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "model": os.getenv("NVIDIA_MODEL_NAME", "nvidia/nemotron-3.5-lightning-30b-a3b"),
    },
    {
        "nombre": "OpenRouter",
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url": os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        "model": os.getenv("LLM_MODEL_NAME", "nvidia/nemotron-3.5-lightning:free"),
    },
    {
        "nombre": "Google AI Studio",
        "api_key_env": "GOOGLE_API_KEY",
        "base_url": os.getenv(
            "GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
        ),
        "model": os.getenv("GOOGLE_MODEL_NAME", "gemini-3.6-flash"),
    },
]


def _es_reintentable(error: Exception) -> bool:
    """True si vale la pena reintentar el MISMO proveedor: 5xx/timeout/red.

    False para errores 4xx (401 key inválida, 404 modelo no existe, 400
    request mal formada) — esos no se arreglan reintentando, hay que pasar
    al siguiente proveedor de inmediato.
    """
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        return True  # error de red/timeout sin respuesta HTTP: vale la pena reintentar
    return status_code >= 500


def _probar_proveedor(proveedor: dict, temperature: float) -> ChatOpenAI | None:
    """Construye el cliente de un proveedor y lo valida con un ping barato.

    Reintenta una vez si el error es transitorio (ver _es_reintentable).
    Devuelve None (sin lanzar excepción) si la key falta o el proveedor no
    responde tras los reintentos, para que build_llm() siga con el siguiente
    de la lista.
    """
    api_key = os.getenv(proveedor["api_key_env"])
    if not api_key:
        return None

    llm = ChatOpenAI(
        model=proveedor["model"],
        base_url=proveedor["base_url"],
        api_key=api_key,
        temperature=temperature,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    for intento in range(1, _MAX_INTENTOS_POR_PROVEEDOR + 1):
        try:
            llm.invoke([HumanMessage(content="ping")])
        except Exception as error:
            if intento < _MAX_INTENTOS_POR_PROVEEDOR and _es_reintentable(error):
                print(
                    f"   REINTENTANDO - {proveedor['nombre']} (intento {intento + 1}/"
                    f"{_MAX_INTENTOS_POR_PROVEEDOR}, error reintentable: {error})..."
                )
                time.sleep(_RETRY_DELAY_SECONDS)
                continue
            print(
                f"   ALERTA - {proveedor['nombre']} no respondió ({error}); "
                "probando el siguiente proveedor..."
            )
            return None
        else:
            print(f"   OK - usando {proveedor['nombre']} (modelo: {proveedor['model']}).")
            return llm
    return None


def build_llm(temperature: float = 0) -> ChatOpenAI:
    """Construye el cliente LLM probando proveedores en orden hasta que uno responda.

    Orden: Groq -> NVIDIA NIM -> OpenRouter -> Google AI Studio.
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
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

        resultado = None
        error_final: Exception | None = None
        for intento in range(1, _MAX_INTENTOS_POR_PROVEEDOR + 1):
            try:
                resultado = llm.with_structured_output(schema, method=method).invoke(messages)
                error_final = None
                break
            except Exception as error:
                error_final = error
                if intento < _MAX_INTENTOS_POR_PROVEEDOR and _es_reintentable(error):
                    print(
                        f"   REINTENTANDO - {proveedor['nombre']} (intento {intento + 1}/"
                        f"{_MAX_INTENTOS_POR_PROVEEDOR}, error reintentable: {error})..."
                    )
                    time.sleep(_RETRY_DELAY_SECONDS)
                    continue
                break

        if error_final is not None:
            print(
                f"   ALERTA - {proveedor['nombre']} lanzó un error generando salida "
                f"estructurada ({error_final}); probando el siguiente proveedor..."
            )
            errores.append(f"{proveedor['nombre']}: {error_final}")
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
