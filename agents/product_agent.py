"""Agente de Producto (product_agent).

Qué hace:
    Interpreta el requerimiento inicial del usuario y genera una especificación
    estructurada del problema a resolver.

Responsabilidad dentro del sistema:
    Primer eslabón del pipeline: convierte lenguaje natural en una definición
    formal que los demás agentes consumen desde el estado compartido
    (EngineeringState) usada por los demás agentes.

Decisiones (Fase 6 de Guia_Construccion.md, agente 1/6):
    - Sin dependencias nuevas más allá del LLM (RAG y MCP llegan en los
      agentes 2+), por eso es el primero en construirse.
    - Structured output vía ProductSpecification (Pydantic): los 4 campos que
      pide graph/state.py (actores, reglas_negocio, criterios_aceptacion,
      riesgos) más `resumen` y `supuestos`, para que el manejo de
      requerimientos ambiguos quede documentado en vez de que el LLM invente
      en silencio.
    - method="function_calling" en with_structured_output en vez del modo
      estricto por defecto: el modelo objetivo es gratuito
      (nemotron-3.5-lightning:free en OpenRouter) y más propenso a rechazar
      json_schema estricto. Si el smoke test falla con un error de schema,
      este es el primer parámetro a revisar.
    - Cliente LLM (_build_llm) como helper privado de este archivo, no un
      módulo compartido: todavía es el único agente que existe. Cuando
      architect_agent.py (agente 2) también necesite un LLM, vale la pena
      extraerlo a un factory común; no se hace ahora para no construir
      infraestructura especulativa.
    - product_agent() devuelve un update parcial del estado (specification,
      messages), coherente con el reducer operator.add de "messages" en
      state.py, para que ya tenga forma de nodo de LangGraph cuando se
      conecte en Fase 7 sin necesidad de reescribirlo.
"""

import json
import os
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    # Permite "python agents/product_agent.py" como script suelto (Fase 6 de
    # la guía): sin esto, sys.path solo contendría agents/, no la raíz del
    # repo, y "from graph.state import ..." fallaría con ModuleNotFoundError.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from graph.state import EngineeringState, create_initial_state
from observability.langfuse_config import flush_traces, observe

load_dotenv()

__all__ = ["ProductSpecification", "product_agent"]

_SYSTEM_PROMPT = """\
Eres un analista de producto senior especializado en levantamiento de requerimientos
para sistemas internos de gestión (ej. flujos de aprobación tipo "Solicitud de
Vacaciones"). Tu trabajo es leer un requerimiento en lenguaje natural, posiblemente
ambiguo o incompleto, y convertirlo en una especificación estructurada que otros
ingenieros (arquitectura, desarrollo, seguridad, testing) usarán como única fuente
de verdad.

Instrucciones:
- Identifica TODOS los actores/roles que participan (no solo el que pide la
  funcionalidad).
- Extrae las reglas de negocio explícitas y las que sean razonablemente implícitas
  en un dominio de gestión empresarial (ej. quién puede aprobar, límites, plazos).
- Redacta criterios de aceptación verificables (evita frases vagas como "debe
  funcionar bien").
- Señala riesgos: casos borde, posibles abusos (ej. autoaprobación), datos
  sensibles, condiciones de carrera.
- Si el requerimiento es ambiguo o le falta información, NO inventes silenciosamente:
  documenta la suposición concreta que tomaste en el campo de supuestos.
- Responde siempre en español, de forma concisa y accionable.
"""


class ProductSpecification(BaseModel):
    """Especificación estructurada que produce el Product Agent."""

    resumen: str = Field(
        ..., description="Reformulación breve (1-2 frases) de lo que pide el requerimiento."
    )
    actores: list[str] = Field(
        ..., description="Roles o tipos de usuario que participan en el flujo."
    )
    reglas_negocio: list[str] = Field(
        ..., description="Reglas de negocio explícitas o implícitas que el sistema debe cumplir."
    )
    criterios_aceptacion: list[str] = Field(
        ..., description="Condiciones verificables que determinan que la funcionalidad está completa."
    )
    riesgos: list[str] = Field(
        ...,
        description="Riesgos funcionales o de seguridad identificados (ambigüedades, casos borde, abuso potencial).",
    )
    supuestos: list[str] = Field(
        default_factory=list,
        description="Supuestos asumidos para resolver ambigüedades del requerimiento; vacío si no hubo.",
    )


def _build_llm() -> ChatOpenAI:
    """Construye el cliente LLM apuntando a OpenRouter vía variables de entorno."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY no está configurada. Copia .env.example a .env "
            "y completa OPENROUTER_API_KEY antes de correr product_agent."
        )
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL_NAME", "nvidia/nemotron-3.5-lightning:free"),
        base_url=os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=api_key,
        temperature=0,
    )


@observe(name="product_agent")
def product_agent(state: EngineeringState) -> dict:
    """Genera la especificación estructurada a partir de state["requirement"].

    Devuelve un update parcial del estado: {"specification": {...}, "messages": [...]}.
    """
    requirement = state["requirement"]
    if not requirement or not requirement.strip():
        raise ValueError("state['requirement'] está vacío; no se puede generar una especificación.")

    llm = _build_llm()
    structured_llm = llm.with_structured_output(ProductSpecification, method="function_calling")

    specification = structured_llm.invoke(
        [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=requirement),
        ]
    )

    return {
        "specification": specification.model_dump(),
        "messages": [
            f"product_agent: especificación generada ({len(specification.actores)} actor(es), "
            f"{len(specification.reglas_negocio)} regla(s), {len(specification.riesgos)} riesgo(s))."
        ],
    }


if __name__ == "__main__":
    print("Fase 6 (agente 1/6) — smoke test de agents/product_agent.py")

    print("1. Verificando OPENROUTER_API_KEY en el entorno...")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit(
            "   ERROR - OPENROUTER_API_KEY no configurada. Completa tu .env "
            "(copia .env.example) antes de correr esta prueba en vivo."
        )
    print("   OK - variable presente.")

    requerimiento_ejemplo = (
        "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
        "y fin, y que mi jefe directo apruebe o rechace la solicitud."
    )
    print(f"2. Construyendo estado inicial con requerimiento de ejemplo:\n   {requerimiento_ejemplo!r}")
    state = create_initial_state(requerimiento_ejemplo)

    print("3. Invocando product_agent (llamada real al LLM vía OpenRouter)...")
    resultado = product_agent(state)

    print("4. Especificación estructurada generada:")
    print(json.dumps(resultado["specification"], indent=2, ensure_ascii=False))

    spec = resultado["specification"]
    checks = {
        "actores": spec.get("actores"),
        "reglas_negocio": spec.get("reglas_negocio"),
        "criterios_aceptacion": spec.get("criterios_aceptacion"),
        "riesgos": spec.get("riesgos"),
    }
    for campo, valor in checks.items():
        flag = "OK" if valor else "ALERTA - vacío"
        print(f"   {flag} - {campo}: {len(valor) if valor else 0} elemento(s)")

    print("5. Forzando flush de traces a Langfuse...")
    flush_traces()

    print("\nListo. Siguiente paso: agents/architect_agent.py (Fase 6, agente 2/6).")

