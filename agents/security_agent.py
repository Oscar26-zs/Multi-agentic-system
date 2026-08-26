"""Agente de Seguridad (security_agent).

Qué hace:
    Lee la propuesta técnica del Architect Agent (y la especificación
    funcional del Product Agent para contexto de roles/riesgos), consulta el
    RAG de seguridad (knowledge/security/) y evalúa el diseño propuesto
    contra las políticas de seguridad internas y el OWASP Top 10 aplicado al
    dominio, devolviendo hallazgos estructurados por severidad.

Responsabilidad dentro del sistema:
    Tercer eslabón del pipeline y control de calidad en seguridad: revisa la
    ARQUITECTURA propuesta (todavía no hay código real — eso lo genera el
    Developer Agent después), citando las guías reales que sustentan cada
    hallazgo en vez de opinar en abstracto.

Decisiones (Fase 6 de Guia_Construccion.md, agente 3/6):
    - RAG de seguridad sí, MCP todavía no: según la tabla de la Fase 6, este
      agente introduce el retriever de seguridad (rag/retrievers.py ->
      get_security_retriever), ya probado aislado en Fase 4, pero NO necesita
      MCP porque revisa una propuesta de arquitectura (texto estructurado),
      no código real del repo — el MCP llega recién con el Developer Agent
      (agente 4/6), que sí opera sobre archivos.
    - Structured output vía SecurityReview (Pydantic), con hallazgos como
      lista de objetos {severidad, categoria_owasp, descripcion,
      recomendacion} en vez de strings sueltos: mismo principio que
      decisiones_tecnicas en architect_agent.py — obliga al LLM a clasificar
      severidad y mapear a una categoría en vez de solo enumerar problemas.
    - riesgos_aceptados como campo separado de hallazgos: knowledge/security
      documenta explícitamente cosas "fuera de alcance del MVP" (ej.
      auditoría de login, recuperación de contraseña). Sin este campo, el LLM
      tiende a reportarlas como hallazgos igual; con él, tiene un lugar
      correcto para reconocerlas sin bloquear el flujo.
    - aprobado se calcula DESPUÉS de la llamada al LLM, contando hallazgos
      con severidad "critica" o "alta" — no se le pide al LLM que se
      autocalifique. Mismo principio que fuentes_consultadas en
      architect_agent.py: un campo derivado y verificable le gana a confiar
      en que el modelo sea consistente entre su lista de hallazgos y su
      propio veredicto.
    - fuentes_consultadas se sobreescribe con los `source` reales devueltos
      por el retriever de seguridad, igual que en architect_agent.py.
    - Cliente LLM vía agents/llm_factory.py (build_llm): tercer agente que lo
      necesita, ver decisión anotada en product_agent.py y architect_agent.py.
"""

import json
import os
import sys
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    # Permite "python agents/security_agent.py" como script suelto (Fase 6
    # de la guía): sin esto, sys.path solo contendría agents/, no la raíz del
    # repo, y "from graph.state import ..." fallaría con ModuleNotFoundError.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agents.llm_factory import invoke_structured
from graph.state import EngineeringState, create_initial_state
from observability.langfuse_config import flush_traces, observe
from rag.retrievers import get_security_retriever

load_dotenv()

__all__ = ["SecurityFinding", "SecurityReview", "security_agent"]

_SEVERIDADES_BLOQUEANTES = {"critica", "alta"}

_SYSTEM_PROMPT = """\
Eres un ingeniero de seguridad senior de un estudio de ingeniería. Recibes la
propuesta técnica que ya redactó el arquitecto (stack, componentes,
decisiones técnicas) más la especificación funcional original (actores,
reglas de negocio, riesgos ya señalados), y fragmentos reales de las guías
internas de seguridad del equipo y del OWASP Top 10 aplicado a este dominio.
Tu trabajo es auditar el DISEÑO propuesto (todavía no hay código escrito) y
señalar cualquier hallazgo de seguridad antes de que el Developer Agent
empiece a construir.

Instrucciones:
- Evalúa control de acceso, autenticación, validación de dónde vive cada
  regla (cliente vs. servidor), manejo de datos sensibles, auditoría y
  condiciones de carrera, apoyándote en el contexto de las guías provisto
  abajo.
- Para cada hallazgo, asigna una severidad (critica, alta, media, baja),
  mapea a una categoría del OWASP Top 10 si aplica (ej. "A01:2021 - Control
  de Acceso Roto"; usa "N/A" si no mapea a ninguna) y da una recomendación
  concreta y accionable para el Developer Agent, no una advertencia vaga.
- Si algo que normalmente sería un hallazgo está explícitamente marcado como
  fuera de alcance del MVP en las guías (ej. auditoría de login, recuperación
  de contraseña), NO lo reportes como hallazgo: regístralo en
  riesgos_aceptados en su lugar.
- No repitas riesgos que la especificación ya documentó como supuesto salvo
  que la arquitectura propuesta los deje sin mitigar.
- Basa cada hallazgo en el contexto de las guías provisto abajo. Si el
  contexto no cubre algo que necesitas evaluar, dilo explícitamente en vez de
  inventar una política que el equipo no tiene.
- Responde siempre en español, de forma concisa y accionable.
"""


class SecurityFinding(BaseModel):
    """Un hallazgo de seguridad individual sobre la arquitectura propuesta."""

    severidad: Literal["critica", "alta", "media", "baja"] = Field(
        ..., description="Severidad del hallazgo."
    )
    categoria_owasp: str = Field(
        ...,
        description="Categoría del OWASP Top 10 aplicable (ej. 'A01:2021 - Control de Acceso Roto'); 'N/A' si no mapea a ninguna.",
    )
    descripcion: str = Field(
        ..., description="Qué se encontró y en qué parte de la arquitectura se manifiesta."
    )
    recomendacion: str = Field(
        ..., description="Cómo mitigarlo, en términos accionables para el Developer Agent."
    )


class SecurityReview(BaseModel):
    """Revisión de seguridad estructurada que produce el Security Agent."""

    resumen: str = Field(
        ..., description="Reformulación breve (1-2 frases) del veredicto de seguridad general."
    )
    hallazgos: list[SecurityFinding] = Field(
        ..., description="Hallazgos de seguridad detectados en la arquitectura propuesta."
    )
    riesgos_aceptados: list[str] = Field(
        default_factory=list,
        description="Riesgos reconocidos pero explícitamente fuera de alcance (según las guías), no bloqueantes.",
    )
    fuentes_consultadas: list[str] = Field(
        default_factory=list,
        description="Nombres de los documentos de knowledge/security/ usados como base (se completa con lo recuperado por RAG).",
    )
    aprobado: bool = Field(
        default=True,
        description="Se recalcula tras la respuesta del LLM: False si hay algún hallazgo crítico o alto.",
    )


def _format_context(docs) -> str:
    """Arma el bloque de contexto RAG a partir de los chunks recuperados."""
    if not docs:
        return "(el retriever de seguridad no devolvió resultados para esta consulta)"
    partes = []
    for doc in docs:
        fuente = doc.metadata.get("source", "desconocida")
        header = doc.metadata.get("header", "")
        etiqueta = f"[{fuente}{' - ' + header if header else ''}]"
        partes.append(f"{etiqueta}\n{doc.page_content}")
    return "\n\n".join(partes)


@observe(name="security_agent")
def security_agent(state: EngineeringState) -> dict:
    """Genera la revisión de seguridad a partir de state["architecture"], apoyada
    en el retriever de seguridad y en state["specification"] como contexto.

    Devuelve un update parcial del estado: {"security_review": {...}, "messages": [...]}.
    """
    architecture = state["architecture"]
    if not architecture:
        raise ValueError(
            "state['architecture'] está vacío; corre architect_agent antes de security_agent."
        )
    specification = state.get("specification", {})

    consulta_rag = " ".join(
        [
            architecture.get("resumen", ""),
            *architecture.get("componentes", []),
            *specification.get("riesgos", []),
        ]
    ).strip()
    if not consulta_rag:
        consulta_rag = "seguridad para la arquitectura provista"

    retriever = get_security_retriever()
    docs = retriever.invoke(consulta_rag)
    contexto = _format_context(docs)
    fuentes = sorted({doc.metadata.get("source") for doc in docs if doc.metadata.get("source")})

    mensaje_usuario = (
        "Especificación funcional (JSON):\n"
        f"{json.dumps(specification, ensure_ascii=False, indent=2)}\n\n"
        "Propuesta de arquitectura a auditar (JSON):\n"
        f"{json.dumps(architecture, ensure_ascii=False, indent=2)}\n\n"
        "Contexto recuperado de las guías internas de seguridad y OWASP:\n"
        f"{contexto}"
    )

    review = invoke_structured(
        SecurityReview,
        [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=mensaje_usuario),
        ],
    )

    security_review = review.model_dump()
    security_review["fuentes_consultadas"] = fuentes or security_review["fuentes_consultadas"]
    security_review["aprobado"] = not any(
        h["severidad"] in _SEVERIDADES_BLOQUEANTES for h in security_review["hallazgos"]
    )

    bloqueantes = sum(
        1 for h in security_review["hallazgos"] if h["severidad"] in _SEVERIDADES_BLOQUEANTES
    )

    return {
        "security_review": security_review,
        "messages": [
            f"security_agent: revisión completada ({len(security_review['hallazgos'])} hallazgo(s), "
            f"{bloqueantes} bloqueante(s), aprobado={security_review['aprobado']}, "
            f"fuentes RAG: {', '.join(fuentes) if fuentes else 'ninguna'})."
        ],
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("Fase 6 (agente 3/6) — smoke test de agents/security_agent.py")

    print("1. Verificando OPENROUTER_API_KEY en el entorno...")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit(
            "   ERROR - OPENROUTER_API_KEY no configurada. Completa tu .env "
            "(copia .env.example) antes de correr esta prueba en vivo."
        )
    print("   OK - variable presente.")

    print("2. Armando un estado con specification + architecture de prueba "
          "(simulando al Product y Architect Agent)...")
    state = create_initial_state(
        "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
        "y fin, y que mi jefe directo apruebe o rechace la solicitud."
    )
    state["specification"] = {
        "resumen": "Flujo de solicitud y aprobación de vacaciones entre empleado y jefe directo.",
        "actores": ["Empleado", "Aprobador", "RRHH"],
        "reglas_negocio": [
            "Un aprobador no puede aprobar su propia solicitud.",
            "La fecha de fin debe ser posterior a la fecha de inicio.",
        ],
        "criterios_aceptacion": [
            "El empleado puede crear una solicitud con fecha de inicio y fin.",
            "El aprobador puede aprobar o rechazar una solicitud pendiente.",
        ],
        "riesgos": ["Auto-aprobación si el aprobador y el autor son el mismo usuario."],
        "supuestos": [],
    }
    state["architecture"] = {
        "resumen": "API REST en ASP.NET Core con controladores por rol y validación de negocio en el dominio.",
        "stack": ["ASP.NET Core", "EF Core", "FluentValidation"],
        "componentes": [
            "Endpoint POST /solicitudes-vacaciones (crear)",
            "Endpoint POST /bandeja-aprobador/{id}/aprobar",
        ],
        "decisiones_tecnicas": [
            {
                "decision": "El id de la solicitud a aprobar se recibe como parámetro de ruta.",
                "justificacion": "Sigue la convención REST del resto del API.",
                "trade_offs": "Requiere validar en servidor que el actor autenticado tiene permiso sobre ese recurso.",
            }
        ],
        "plan_alto_nivel": ["Implementar entidad SolicitudVacaciones", "Implementar endpoints de creación y aprobación"],
        "riesgos_tecnicos": ["Condición de carrera si dos aprobaciones llegan al mismo tiempo."],
        "fuentes_consultadas": [],
    }

    print("3. Verificando que el retriever de seguridad devuelva resultados (rag/ ya poblado)...")
    docs_check = get_security_retriever().invoke(state["architecture"]["resumen"])
    if not docs_check:
        raise SystemExit(
            "   ERROR - 0 resultados del retriever de seguridad. Corre primero "
            "'python rag/ingestion.py' para poblar el vector store."
        )
    print(f"   OK - {len(docs_check)} fragmento(s) recuperado(s).")

    print("4. Invocando security_agent (llamada real al LLM vía OpenRouter)...")
    resultado = security_agent(state)

    print("5. Revisión de seguridad generada:")
    print(json.dumps(resultado["security_review"], indent=2, ensure_ascii=False))

    review = resultado["security_review"]
    checks = {
        "hallazgos": review.get("hallazgos"),
        "fuentes_consultadas": review.get("fuentes_consultadas"),
    }
    for campo, valor in checks.items():
        flag = "OK" if valor else "ALERTA - vacío"
        print(f"   {flag} - {campo}: {len(valor) if valor else 0} elemento(s)")
    print(f"   OK - aprobado: {review.get('aprobado')}")

    print("6. Forzando flush de traces a Langfuse...")
    flush_traces()

    print("\nListo. Siguiente paso: agents/developer_agent.py (Fase 6, agente 4/6).")
