"""Agente Arquitecto (architect_agent).

Qué hace:
    Lee la especificación del Product Agent, consulta el RAG de arquitectura
    (knowledge/architecture/) y genera una propuesta técnica estructurada:
    stack, componentes, decisiones técnicas (con trade-offs) y plan de alto
    nivel para el Developer Agent.

Responsabilidad dentro del sistema:
    Segundo eslabón del pipeline. Define el "cómo": traduce la especificación
    funcional (`specification`) en una arquitectura concreta (`architecture`),
    citando las guías reales que la sustentan en vez de inventar convenciones
    propias.

Decisiones (Fase 6 de Guia_Construccion.md, agente 2/6):
    - Primera dependencia nueva del pipeline: el retriever de arquitectura
      (rag/retrievers.py -> get_architecture_retriever), ya probado aislado
      en Fase 4. Si este agente falla, el smoke test primero descarta que el
      problema sea del RAG (0 resultados) antes de sospechar del LLM.
    - Structured output vía ArchitectureProposal (Pydantic), con
      decisiones_tecnicas como lista de objetos {decision, justificacion,
      trade_offs} en vez de strings sueltos: obliga al LLM a justificar cada
      decisión y declarar su costo, no solo enumerarlas.
    - fuentes_consultadas se sobreescribe DESPUÉS de la llamada al LLM con
      los `source` reales devueltos por el retriever (no se confía en que el
      LLM cite bien solo desde el prompt): así el campo siempre refleja qué
      chunks entraron al contexto, aunque el LLM olvide mencionarlos.
    - Cliente LLM vía agents/llm_factory.py (build_llm): con security_agent.py
      (agente 3/6) llegó el tercer agente que necesita un cliente LLM, así
      que la copia local de _build_llm() que tenía este archivo se extrajo al
      factory común en vez de seguir duplicándose.
    - method="function_calling" y temperature=0, mismas razones que
      product_agent.py (modelo gratuito de OpenRouter más tolerante a ese
      modo; salida determinista para un documento técnico).
"""

import json
import os
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    # Permite "python agents/architect_agent.py" como script suelto (Fase 6
    # de la guía): sin esto, sys.path solo contendría agents/, no la raíz del
    # repo, y "from graph.state import ..." fallaría con ModuleNotFoundError.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agents.llm_factory import build_llm
from graph.state import EngineeringState, create_initial_state
from observability.langfuse_config import flush_traces, observe
from rag.retrievers import get_architecture_retriever

load_dotenv()

__all__ = ["ArchitectureProposal", "TechnicalDecision", "architect_agent"]

_SYSTEM_PROMPT = """\
Eres un arquitecto de software senior de un estudio de ingeniería. Recibes la
especificación funcional que ya redactó el analista de producto (actores,
reglas de negocio, criterios de aceptación, riesgos) más fragmentos reales de
las guías internas de arquitectura del equipo. Tu trabajo es proponer un
diseño técnico concreto para ese requerimiento, sin salirte de lo que dicen
esas guías.

Instrucciones:
- Propón el stack (lenguajes, frameworks, librerías) y los componentes
  principales (ej. capas, servicios, módulos) necesarios para cubrir la
  especificación.
- Para cada decisión técnica relevante, documenta también su trade-off (qué
  se gana y qué se sacrifica), no solo la decisión en sí.
- El plan de alto nivel debe ser una secuencia de pasos de implementación
  ejecutables por el Developer Agent, no una descripción vaga.
- Señala riesgos técnicos (no funcionales): cuellos de botella, puntos únicos
  de falla, dificultad de testing, deuda técnica que se estaría aceptando.
- Basa tus decisiones en el contexto de las guías de arquitectura provisto
  abajo. Si el contexto no cubre algo que necesitas decidir, dilo
  explícitamente en vez de inventar una convención que el equipo no tiene.
- Responde siempre en español, de forma concisa y accionable.
"""


class TechnicalDecision(BaseModel):
    """Una decisión técnica individual, con su costo explícito."""

    decision: str = Field(..., description="La decisión técnica tomada (ej. 'usar un job asíncrono para X').")
    justificacion: str = Field(..., description="Por qué esta decisión resuelve la especificación o sigue una guía interna.")
    trade_offs: str = Field(..., description="Qué se sacrifica o qué riesgo se acepta al tomar esta decisión.")


class ArchitectureProposal(BaseModel):
    """Propuesta técnica estructurada que produce el Architect Agent."""

    resumen: str = Field(
        ..., description="Reformulación breve (1-2 frases) del enfoque técnico propuesto."
    )
    stack: list[str] = Field(
        ..., description="Lenguajes, frameworks y librerías propuestos para implementar la especificación."
    )
    componentes: list[str] = Field(
        ..., description="Componentes/módulos/capas principales del diseño (ej. 'endpoint de aprobación', 'tabla solicitudes')."
    )
    decisiones_tecnicas: list[TechnicalDecision] = Field(
        ..., description="Decisiones técnicas clave, cada una con su justificación y trade-off."
    )
    plan_alto_nivel: list[str] = Field(
        ..., description="Pasos de implementación, en orden, para que el Developer Agent los ejecute."
    )
    riesgos_tecnicos: list[str] = Field(
        ..., description="Riesgos técnicos identificados: cuellos de botella, puntos únicos de falla, deuda técnica."
    )
    fuentes_consultadas: list[str] = Field(
        default_factory=list,
        description="Nombres de los documentos de knowledge/architecture/ usados como base (se completa con lo recuperado por RAG).",
    )


def _format_context(docs) -> str:
    """Arma el bloque de contexto RAG a partir de los chunks recuperados."""
    if not docs:
        return "(el retriever de arquitectura no devolvió resultados para esta consulta)"
    partes = []
    for doc in docs:
        fuente = doc.metadata.get("source", "desconocida")
        header = doc.metadata.get("header", "")
        etiqueta = f"[{fuente}{' - ' + header if header else ''}]"
        partes.append(f"{etiqueta}\n{doc.page_content}")
    return "\n\n".join(partes)


@observe(name="architect_agent")
def architect_agent(state: EngineeringState) -> dict:
    """Genera la propuesta técnica a partir de state["specification"], apoyada
    en el retriever de arquitectura.

    Devuelve un update parcial del estado: {"architecture": {...}, "messages": [...]}.
    """
    specification = state["specification"]
    if not specification:
        raise ValueError(
            "state['specification'] está vacío; corre product_agent antes de architect_agent."
        )

    consulta_rag = " ".join(
        [
            specification.get("resumen", ""),
            *specification.get("reglas_negocio", []),
        ]
    ).strip()
    if not consulta_rag:
        consulta_rag = "arquitectura para la especificación provista"

    retriever = get_architecture_retriever()
    docs = retriever.invoke(consulta_rag)
    contexto = _format_context(docs)
    fuentes = sorted({doc.metadata.get("source") for doc in docs if doc.metadata.get("source")})

    llm = build_llm()
    structured_llm = llm.with_structured_output(ArchitectureProposal, method="function_calling")

    mensaje_usuario = (
        "Especificación funcional (JSON):\n"
        f"{json.dumps(specification, ensure_ascii=False, indent=2)}\n\n"
        "Contexto recuperado de las guías internas de arquitectura:\n"
        f"{contexto}"
    )

    proposal = structured_llm.invoke(
        [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=mensaje_usuario),
        ]
    )

    architecture = proposal.model_dump()
    architecture["fuentes_consultadas"] = fuentes or architecture["fuentes_consultadas"]

    return {
        "architecture": architecture,
        "messages": [
            f"architect_agent: propuesta generada ({len(architecture['componentes'])} componente(s), "
            f"{len(architecture['decisiones_tecnicas'])} decisión(es), "
            f"fuentes RAG: {', '.join(fuentes) if fuentes else 'ninguna'})."
        ],
    }


if __name__ == "__main__":
    print("Fase 6 (agente 2/6) — smoke test de agents/architect_agent.py")

    print("1. Verificando OPENROUTER_API_KEY en el entorno...")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit(
            "   ERROR - OPENROUTER_API_KEY no configurada. Completa tu .env "
            "(copia .env.example) antes de correr esta prueba en vivo."
        )
    print("   OK - variable presente.")

    print("2. Armando un estado con una specification de prueba (simulando al Product Agent)...")
    state = create_initial_state(
        "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
        "y fin, y que mi jefe directo apruebe o rechace la solicitud."
    )
    state["specification"] = {
        "resumen": "Flujo de solicitud y aprobación de vacaciones entre empleado y jefe directo.",
        "actores": ["Empleado", "Jefe directo"],
        "reglas_negocio": [
            "Un empleado no puede aprobar su propia solicitud.",
            "La fecha de fin debe ser posterior a la fecha de inicio.",
        ],
        "criterios_aceptacion": [
            "El empleado puede crear una solicitud con fecha de inicio y fin.",
            "El jefe directo puede aprobar o rechazar una solicitud pendiente.",
        ],
        "riesgos": ["Autoaprobación si el jefe directo no está bien definido en el sistema."],
        "supuestos": ["Cada empleado tiene exactamente un jefe directo asignado."],
    }

    print("3. Verificando que el retriever de arquitectura devuelva resultados (rag/ ya poblado)...")
    docs_check = get_architecture_retriever().invoke(state["specification"]["resumen"])
    if not docs_check:
        raise SystemExit(
            "   ERROR - 0 resultados del retriever de arquitectura. Corre primero "
            "'python rag/ingestion.py' para poblar el vector store."
        )
    print(f"   OK - {len(docs_check)} fragmento(s) recuperado(s).")

    print("4. Invocando architect_agent (llamada real al LLM vía OpenRouter)...")
    resultado = architect_agent(state)

    print("5. Propuesta técnica generada:")
    print(json.dumps(resultado["architecture"], indent=2, ensure_ascii=False))

    arch = resultado["architecture"]
    checks = {
        "stack": arch.get("stack"),
        "componentes": arch.get("componentes"),
        "decisiones_tecnicas": arch.get("decisiones_tecnicas"),
        "plan_alto_nivel": arch.get("plan_alto_nivel"),
        "fuentes_consultadas": arch.get("fuentes_consultadas"),
    }
    for campo, valor in checks.items():
        flag = "OK" if valor else "ALERTA - vacío"
        print(f"   {flag} - {campo}: {len(valor) if valor else 0} elemento(s)")

    print("6. Forzando flush de traces a Langfuse...")
    flush_traces()

    print("\nListo. Siguiente paso: agents/security_agent.py (Fase 6, agente 3/6).")
