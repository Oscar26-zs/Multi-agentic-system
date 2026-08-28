"""Agente Desarrollador (developer_agent).

Qué hace:
    Lee la propuesta técnica del Architect Agent (y la especificación
    funcional como contexto de negocio), consulta el RAG de desarrollo
    (knowledge/development/) y en dos fases contra el servidor MCP real:
    (1) explora el repositorio real lo mínimo necesario y (2) ejecuta un plan
    estructurado y compacto de cambios de archivo — sin ciclo abierto de
    tool-calling para la escritura.

Responsabilidad dentro del sistema:
    Único agente autorizado a modificar código del repositorio objetivo.
    Todas sus operaciones sobre archivos pasan por las tools MCP (sandboxeadas
    a REPO_TARGET_PATH); nunca toca el filesystem directamente.

Decisiones (Fase 6 de Guia_Construccion.md, agente 4/6):
    - MCP sí, sin `security_review` como dependencia: según el pipeline real
      del grafo (README.md: Product -> Architect -> Developer -> Security ->
      Testing -> Reviewer) y el orden de campos de graph/state.py
      (`architecture` antes que `implementation`, `implementation` antes que
      `security_review`), el Developer Agent corre ANTES que el Security
      Agent — `state["security_review"]` todavía no existe en ese punto del
      flujo. Por eso este agente solo depende de `architecture` (obligatoria)
      y `specification` (opcional, como contexto de negocio).
    - Conexión MCP vía protocolo real (stdio_client + ClientSession), no
      llamando las funciones de mcp_server/server.py directamente: así el
      agente ejercita exactamente el mismo camino que un cliente MCP externo,
      y las tools quedan sandboxeadas del lado del servidor.
    - REDISEÑO: "planificar" separado de "ejecutar" (agents/mcp_tools.py ->
      run_exploration_loop / apply_file_changes). El ciclo ReAct original
      (un solo hilo de conversación que exploraba Y escribía, turno a turno)
      hacía crecer el historial acumulado hasta romper el límite de
      tokens/minuto del proveedor gratuito (413 "Request too large"), y
      dejaba la escritura real a merced de que el LLM no alucinara una tool
      a mitad de una conversación larga (pasó de verdad: "print_tree", que
      nunca se le ofreció). Ahora:
        1. run_exploration_loop() corre un ciclo CORTO (MAX_EXPLORATION_TURNS)
           bindeado SOLO con tools de lectura — create_file/update_file ni
           siquiera están disponibles en esta fase, así que la escritura
           queda estructuralmente imposible de alucinar acá.
        2. Con ese contexto, UNA sola llamada a invoke_structured() pide un
           plan compacto (`DeveloperPlan`): la lista completa de cambios de
           archivo, no más conversación abierta.
        3. apply_file_changes() ejecuta esa lista iterando en Python — CERO
           llamadas LLM adicionales para la parte mecánica de escribir.
      Resultado: muchas menos llamadas LLM por corrida, contexto que nunca
      vuelve a crecer sin límite, y la fase de escritura ya no puede
      alucinar una tool porque no es el LLM quien la ejecuta.
    - `resumen`/`notas` ahora vienen del propio `DeveloperPlan` (redactados
      ANTES de ejecutar) — se eliminó la llamada final aparte a un
      "ImplementationSummary" que existía en el diseño anterior, porque ya
      no hace falta: no hay una conversación posterior de la que resumir "lo
      que pasó", el resultado de la ejecución ya es 100% determinista
      (`archivos_creados`/`archivos_modificados`/`diff`/`pasos_seguidos`, ver
      abajo). Si algún cambio puntual falla al ejecutarse, queda registrado
      en `pasos_seguidos` de forma determinista, no se le vuelve a preguntar
      al LLM qué pasó.
    - `archivos_creados`, `archivos_modificados`, `diff` y `pasos_seguidos`
      se calculan en Python a partir de lo que `apply_file_changes()`
      realmente ejecutó contra el MCP — nunca se le pide al LLM que los
      recuerde de memoria. Mismo principio que `fuentes_consultadas` en
      architect_agent.py y `aprobado` en security_agent.py.
    - `diff` se arma con `difflib.unified_diff` (agents/mcp_tools.py) sobre
      el `contenido` propuesto (`crear`) o `old_text`/`new_text` (`editar`),
      no con una tool `get_diff` (no existe en mcp_server/server.py).
    - Cliente LLM vía agents/llm_factory.py (build_llm/invoke_structured),
      igual que los demás agentes.
    - print() por fase/turno/cambio: corriendo vía app.py (streaming por
      nodo), la terminal quedaba en silencio total durante toda la corrida de
      este agente — con esto se ve cada paso en tiempo real.
    - MAX_EXPLORATION_TURNS (antes MAX_TOOL_ITERATIONS) ahora limita SOLO la
      fase de exploración (ya no también la escritura, que no tiene límite de
      turnos propio — apply_file_changes() ejecuta todos los cambios del plan
      de una vez). Se puede mantener bajo (5) porque ya no tiene que cubrir
      también la escritura.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    # Permite "python agents/developer_agent.py" como script suelto (Fase 6
    # de la guía): sin esto, sys.path solo contendría agents/, no la raíz del
    # repo, y "from graph.state import ..." fallaría con ModuleNotFoundError.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_factory import build_llm, invoke_structured
from agents.mcp_tools import (
    REPO_ROOT,
    FileChange,
    apply_file_changes,
    mcp_server_params,
    run_exploration_loop,
    summarize_exploration,
)
from graph.state import EngineeringState, create_initial_state
from observability.langfuse_config import flush_traces, observe
from rag.retrievers import get_development_retriever

load_dotenv()

__all__ = ["DeveloperPlan", "developer_agent"]

MAX_EXPLORATION_TURNS = 5  # cota de la fase de exploración (solo lectura); la
# escritura ya no tiene ciclo de turnos propio, se ejecuta de una vez.

_EXPLORATION_PROMPT = """\
Eres un ingeniero de software senior de un estudio de ingeniería. Recibís la
propuesta técnica que ya redactó el arquitecto (stack, componentes,
decisiones técnicas, plan de alto nivel) más fragmentos reales de las guías
internas de desarrollo del equipo.

Estás en la FASE DE EXPLORACIÓN, no en la de escritura: en este turno solo
tenés disponibles tools de LECTURA (list_files, read_file, search_code) — no
existe create_file ni update_file en esta fase, ni ninguna otra tool bajo
ningún nombre (ej. NO existe "print_tree" ni variantes; list_files() ya da el
árbol completo y recursivo en un solo llamado).

Tu único objetivo es juntar el contexto mínimo necesario para poder proponer
después los cambios de archivo concretos: confirmá rutas reales, convenciones
del proyecto (nombres, capas) y, para cualquier archivo que vayas a EDITAR
(no crear), su contenido EXACTO — vas a necesitar ese texto literal para
proponer un reemplazo que funcione.

No repitas una tool con los mismos argumentos si ya tenés esa información. Si
una llamada falla (ERROR), NUNCA la reintentes igual — cambiá de estrategia.
Cuando tengas contexto suficiente (normalmente alcanza con pocas llamadas),
dejá de pedir tools y respondé con texto breve confirmando que estás listo.
Responde siempre en español.
"""

_PLAN_SYSTEM_PROMPT = """\
Eres un ingeniero de software senior de un estudio de ingeniería. En este
paso tu único trabajo es análisis y planificación — no ejecutás nada
directamente, solo respondés con el plan estructurado que se te pide, basado
en la información que se te da a continuación.
"""

_PLAN_PROMPT = """\
Con base en la exploración ya realizada (arriba), generá la lista COMPLETA
de cambios de archivo necesarios para cumplir el `plan_alto_nivel` de la
arquitectura — ni más ni menos de lo que hace falta para que el
requerimiento funcione. Si la especificación es trivial, el plan también
debe serlo: no agregues archivos, capas ni indirection que el requerimiento
no pide.

Para cada cambio:
- accion="crear": `contenido` debe ser el archivo completo y funcional.
- accion="editar": `old_text` debe ser el fragmento EXACTO que ya se leyó
  arriba (mismos espacios/indentación) — si el contenido real de ese archivo
  no aparece en la exploración de arriba, no propongas editarlo (proponé
  crearlo si corresponde, o dejalo fuera del plan).
- `razon`: por qué hace falta este cambio puntual.

No propongas cambios sobre archivos que no aparecen en la exploración de
arriba. `resumen` describe qué se va a implementar y cómo; `notas` son
desviaciones del plan de arquitectura o limitaciones conocidas (vacío si no
hay). Responde siempre en español.
"""


class DeveloperPlan(BaseModel):
    """Plan estructurado y compacto de cambios de archivo — reemplaza al
    ciclo abierto de tool-calling para la fase de escritura: el código
    ejecuta esta lista directo contra el MCP (agents/mcp_tools.py ->
    apply_file_changes), sin ningún LLM adicional."""

    cambios: list[FileChange] = Field(
        default_factory=list,
        description="Cambios de archivo necesarios; puede ir vacía si de verdad no hace falta tocar nada.",
    )
    resumen: str = Field(..., description="Resumen breve (2-4 frases) de qué se va a implementar y cómo.")
    notas: list[str] = Field(
        default_factory=list,
        description="Desviaciones del plan de arquitectura o limitaciones conocidas; vacío si no hubo.",
    )


def _format_context(docs) -> str:
    """Arma el bloque de contexto RAG a partir de los chunks recuperados."""
    if not docs:
        return "(el retriever de desarrollo no devolvió resultados para esta consulta)"
    partes = []
    for doc in docs:
        fuente = doc.metadata.get("source", "desconocida")
        header = doc.metadata.get("header", "")
        etiqueta = f"[{fuente}{' - ' + header if header else ''}]"
        partes.append(f"{etiqueta}\n{doc.page_content}")
    return "\n\n".join(partes)


def _format_diffs(diffs: dict) -> str:
    if not diffs:
        return "(sin cambios de archivo aplicados)"
    return "\n\n".join(diffs[path] for path in sorted(diffs))


async def _plan_and_apply(
    mensaje_usuario: str,
) -> tuple[list[str], list[str], dict, list[str], str, list[str]]:
    """Explora corto, pide un plan estructurado y lo ejecuta sin LLM adicional.

    Devuelve (archivos_creados, archivos_modificados, diffs, pasos, resumen, notas).
    """
    async with stdio_client(mcp_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            llm = build_llm()

            messages = await run_exploration_loop(
                session, llm, _EXPLORATION_PROMPT, mensaje_usuario,
                MAX_EXPLORATION_TURNS, "Developer Agent",
            )

            print("      [Developer Agent] generando plan de cambios...")
            resumen_exploracion = summarize_exploration(messages)
            plan_context = (
                f"Requerimiento original:\n{mensaje_usuario}\n\n"
                f"--- Exploración ya realizada ---\n{resumen_exploracion}\n\n{_PLAN_PROMPT}"
            )
            try:
                # Contexto NUEVO de 2 mensajes (System + Human), sin los
                # AIMessage con tool_calls de la exploración — ver la decisión
                # en agents/mcp_tools.py::summarize_exploration.
                plan = invoke_structured(
                    DeveloperPlan,
                    [SystemMessage(content=_PLAN_SYSTEM_PROMPT), HumanMessage(content=plan_context)],
                )
            except Exception as error:
                # Ningún proveedor logró devolver el plan (los 4 fallaron, o el
                # único configurado falló) — no debe tumbar todo el pipeline
                # por esto: se sigue con un plan vacío y una nota explícita,
                # mismo criterio que el resto del sistema ante un error del
                # LLM que no se puede resolver.
                print(f"      [Developer Agent] ALERTA - no se pudo generar el plan de cambios: {error}")
                return [], [], {}, [f"⚠ no se pudo generar el plan de cambios: {error}"], (
                    "No se generó ninguna implementación: todos los proveedores LLM "
                    "fallaron al pedir el plan de cambios."
                ), []
            print(f"      [Developer Agent] plan con {len(plan.cambios)} cambio(s); ejecutando...")

            archivos_creados, archivos_modificados, diffs, pasos = await apply_file_changes(
                session, plan.cambios, "Developer Agent"
            )
            if not plan.cambios:
                pasos.append("⚠ el plan no propuso ningún cambio de archivo.")

            return archivos_creados, archivos_modificados, diffs, pasos, plan.resumen, plan.notas


@observe(name="developer_agent")
def developer_agent(state: EngineeringState) -> dict:
    """Implementa state["architecture"] sobre el repo real vía MCP.

    Devuelve un update parcial del estado: {"implementation": {...}, "messages": [...]}.
    """
    architecture = state["architecture"]
    if not architecture:
        raise ValueError(
            "state['architecture'] está vacío; corre architect_agent antes de developer_agent."
        )
    specification = state.get("specification", {})

    consulta_rag = " ".join(
        [
            architecture.get("resumen", ""),
            *architecture.get("componentes", []),
            *architecture.get("plan_alto_nivel", []),
        ]
    ).strip()
    if not consulta_rag:
        consulta_rag = "estándares de desarrollo para la arquitectura provista"

    retriever = get_development_retriever()
    docs = retriever.invoke(consulta_rag)
    contexto = _format_context(docs)
    fuentes = sorted({doc.metadata.get("source") for doc in docs if doc.metadata.get("source")})

    mensaje_usuario = (
        "Especificación funcional, como contexto de negocio (JSON):\n"
        f"{json.dumps(specification, ensure_ascii=False, indent=2)}\n\n"
        "Propuesta de arquitectura a implementar (JSON):\n"
        f"{json.dumps(architecture, ensure_ascii=False, indent=2)}\n\n"
        "Contexto recuperado de las guías internas de desarrollo:\n"
        f"{contexto}"
    )

    archivos_creados, archivos_modificados, diffs, pasos, resumen, notas = asyncio.run(
        _plan_and_apply(mensaje_usuario)
    )

    implementation = {
        "resumen": resumen,
        "notas": notas,
        "archivos_creados": archivos_creados,
        "archivos_modificados": archivos_modificados,
        "diff": _format_diffs(diffs),
        "pasos_seguidos": pasos,
        "fuentes_consultadas": fuentes,
    }

    return {
        "implementation": implementation,
        "messages": [
            f"developer_agent: implementación completada ({len(archivos_creados)} archivo(s) creado(s), "
            f"{len(archivos_modificados)} modificado(s), {len(pasos)} acción(es) MCP, "
            f"fuentes RAG: {', '.join(fuentes) if fuentes else 'ninguna'})."
        ],
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("Fase 6 (agente 4/6) — smoke test de agents/developer_agent.py")

    print("1. Verificando OPENROUTER_API_KEY en el entorno...")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit(
            "   ERROR - OPENROUTER_API_KEY no configurada. Completa tu .env "
            "(copia .env.example) antes de correr esta prueba en vivo."
        )
    print("   OK - variable presente.")

    print("2. Verificando que REPO_TARGET_PATH exista (clon local del MVC)...")
    repo_target = os.getenv("REPO_TARGET_PATH", "")
    if not repo_target or not (REPO_ROOT / repo_target).resolve().is_dir():
        raise SystemExit(
            f"   ERROR - REPO_TARGET_PATH={repo_target!r} no existe. Clona tu repo MVC "
            "en esa ruta (o ajusta REPO_TARGET_PATH en .env) antes de correr esta prueba."
        )
    print(f"   OK - {repo_target!r} existe.")

    print("3. Armando un estado con architecture de prueba (simulando al Architect Agent)...")
    requerimiento_ejemplo = sys.argv[1] if len(sys.argv) > 1 else (
        "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio "
        "y fin, y que mi jefe directo apruebe o rechace la solicitud."
    )
    state = create_initial_state(requerimiento_ejemplo)
    if len(sys.argv) > 1:
        print(
            "   (nota: requirement personalizado; la architecture de prueba de abajo "
            "sigue siendo la del ejemplo de vacaciones, no se deriva de tu texto)"
        )
    state["specification"] = {
        "resumen": "Flujo de solicitud y aprobación de vacaciones entre empleado y aprobador.",
        "actores": ["Empleado", "Aprobador"],
        "reglas_negocio": ["Un aprobador no puede aprobar su propia solicitud."],
        "criterios_aceptacion": ["El empleado puede crear una solicitud con fecha de inicio y fin."],
        "riesgos": ["Auto-aprobación si el aprobador y el autor son el mismo usuario."],
        "supuestos": [],
    }
    state["architecture"] = {
        "resumen": "Documentar en el repo el criterio de aceptación de auto-aprobación, sin tocar código de producción todavía.",
        "stack": ["Markdown"],
        "componentes": ["Nota de documentación de cambios"],
        "decisiones_tecnicas": [
            {
                "decision": "Crear _mcp_scratch/smoke_test_developer_agent.md documentando el criterio de auto-aprobación.",
                "justificacion": "Permite validar el ciclo MCP real (list_files/create_file) sin modificar código de producción del MVC.",
                "trade_offs": "No implementa código real; es solo para el smoke test de este agente.",
            }
        ],
        "plan_alto_nivel": [
            "Listar los archivos en la raíz del repositorio con list_files.",
            "Crear _mcp_scratch/smoke_test_developer_agent.md con una nota breve sobre la regla de auto-aprobación.",
        ],
        "riesgos_tecnicos": [],
        "fuentes_consultadas": [],
    }

    print("4. Verificando que el retriever de desarrollo devuelva resultados (rag/ ya poblado)...")
    docs_check = get_development_retriever().invoke(state["architecture"]["resumen"])
    if not docs_check:
        raise SystemExit(
            "   ERROR - 0 resultados del retriever de desarrollo. Corre primero "
            "'python rag/ingestion.py' para poblar el vector store."
        )
    print(f"   OK - {len(docs_check)} fragmento(s) recuperado(s).")

    print("5. Invocando developer_agent (llamadas reales al LLM y al servidor MCP)...")
    scratch_dir = (REPO_ROOT / repo_target).resolve() / "_mcp_scratch"
    try:
        resultado = developer_agent(state)

        print("6. Implementación generada:")
        print(json.dumps(resultado["implementation"], indent=2, ensure_ascii=False))

        impl = resultado["implementation"]
        checks = {
            "archivos_creados": impl.get("archivos_creados"),
            "pasos_seguidos": impl.get("pasos_seguidos"),
        }
        for campo, valor in checks.items():
            flag = "OK" if valor else "ALERTA - vacío"
            print(f"   {flag} - {campo}: {len(valor) if valor else 0} elemento(s)")
    finally:
        # En un try/finally para que el scratch quede limpio incluso si el
        # paso de impresión falla (ej. consola Windows sin UTF-8) — de lo
        # contrario un archivo del _mcp_scratch anterior queda "ya existe"
        # para la siguiente corrida, confundiendo al LLM en el próximo intento.
        print("7. Limpiando el scratch de la prueba (_mcp_scratch/)...")
        if scratch_dir.is_dir():
            import shutil

            shutil.rmtree(scratch_dir, ignore_errors=True)
            print("   OK - _mcp_scratch/ eliminado.")

    print("8. Forzando flush de traces a Langfuse...")
    flush_traces()

    print("\nListo. Siguiente paso: agents/testing_agent.py (Fase 6, agente 5/6).")
