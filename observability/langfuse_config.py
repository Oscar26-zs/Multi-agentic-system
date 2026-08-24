"""Configuración de Langfuse.

Qué hace:
    Centraliza la inicialización del cliente de Langfuse y el trazado de la
    ejecución del sistema multiagente.

Responsabilidad dentro del sistema:
    Da visibilidad end-to-end: qué prompt usó cada agente, tokens consumidos,
    latencia por nodo y errores ocurridos, tanto en desarrollo como en
    producción.

Qué contiene (Fase 2 de Guia_Construccion.md — esqueleto antes que los
agentes, para que cada uno nazca ya instrumentado):
    - Carga de .env dentro del propio módulo (autosuficiente).
    - get_langfuse_client(): singleton del cliente, con auth_check() para
      validar credenciales.
    - observe: decorador nativo del SDK, re-exportado desde este módulo para
      que los agentes lo importen desde un solo punto propio del proyecto.
    - get_callback_handler(): handler de LangChain/LangGraph para tracing
      automático de prompts, tokens, latencia y costo de cada llamada LLM.
    - flush_traces(): fuerza el envío del batch async antes de que termine
      un proceso corto (scripts de prueba, if __name__ == "__main__").
"""

from dotenv import load_dotenv
from langfuse import get_client, observe
from langfuse.langchain import CallbackHandler

load_dotenv()

__all__ = [
    "observe",
    "get_langfuse_client",
    "auth_check",
    "get_callback_handler",
    "flush_traces",
]


def get_langfuse_client():
    """Devuelve el cliente Langfuse singleton (credenciales desde el entorno).

    get_client() del SDK ya cachea la instancia internamente, por lo que
    llamarlo varias veces desde distintos agentes no reconstruye el cliente.
    """
    return get_client()


def auth_check() -> bool:
    """Valida las credenciales de Langfuse contra la nube.

    Falla rápido y con un mensaje claro si el .env está mal configurado, en
    vez de que el primer agente truene de forma confusa más adelante.
    """
    return get_langfuse_client().auth_check()


def get_callback_handler() -> CallbackHandler:
    """Handler para pasar en config={"callbacks": [...]} a cadenas/grafos.

    Traza automáticamente cada llamada LLM que ocurra dentro de la
    invocación (prompt, tokens, latencia, costo), complementando a @observe
    (que traza la función del agente como span, no lo que pasa adentro).
    """
    return CallbackHandler()


def flush_traces() -> None:
    """Envía inmediatamente los eventos pendientes (batch async del SDK).

    Necesario en scripts cortos que terminan antes de que el batch se envíe
    solo; no hace falta en procesos de larga duración (ej. una API corriendo).
    """
    get_langfuse_client().flush()


if __name__ == "__main__":
    print("Fase 2 — smoke test de observability/langfuse_config.py")

    print("1. Verificando credenciales contra Langfuse cloud...")
    if not auth_check():
        raise SystemExit(
            "auth_check() fallo: revisa LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST en tu .env"
        )
    print("   OK - credenciales válidas.")

    print("2. Ejecutando función dummy decorada con @observe...")

    @observe(name="fase2-smoke-test")
    def dummy_traced_function() -> str:
        return "hola desde fase 2"

    resultado = dummy_traced_function()
    print(f"   OK - función ejecutada, devolvió: {resultado!r}")

    print("3. Forzando flush de traces...")
    flush_traces()
    print("   OK - traces enviados.")

    print(
        "\nListo. Entra a cloud.langfuse.com -> tu proyecto -> Traces y "
        "confirma que aparece un trace llamado 'fase2-smoke-test'."
    )
