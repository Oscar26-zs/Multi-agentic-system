"""Punto de entrada de la aplicación (Fase 9 de Guia_Construccion.md).

Qué hace:
    Recibe el requerimiento del usuario (por argumento o de forma
    interactiva), lo inyecta en el workflow compilado de LangGraph
    (graph/workflow.py) y muestra el resultado final: veredicto, expediente
    completo por etapa y métricas del ciclo de revisión.

Responsabilidad dentro del sistema:
    Interfaz externa de orquestación; no contiene lógica de agentes. Solo
    parsea input, invoca el grafo ya construido y presenta lo que devuelve —
    toda la lógica real vive en agents/ y graph/, no acá.

Decisiones (Fase 9 de Guia_Construccion.md):
    - CLI vía argparse, no FastAPI ni Streamlit: la guía es explícita en que
      "app.py no hace nada nuevo, solo expone workflow.py" — agregar un
      framework web/UI sería la primera pieza de infraestructura de este
      proyecto que no está pedida por ningún caso de uso real todavía.
      requirements.txt tampoco trae fastapi/streamlit.
    - build_graph() se llama una vez por ejecución (no se importa el
      graph_app ya compilado a nivel de módulo de graph/workflow.py):
      mantiene app.py como "capa delgada" que usa la factory pública tal
      como la diseñó graph/workflow.py, en vez de acoplarse a un singleton.
    - Los errores de un agente (NodeExecutionError, graph/nodes.py) se
      capturan acá y se reportan con el nombre del nodo que falló, en vez de
      dejar que el traceback crudo de LangGraph/Pydantic llegue al usuario
      final de la CLI.
    - Código de salida distinto según el desenlace (0=APPROVED, 1=REJECTED
      sin escalar, 2=escalado a humano, 3=cancelado por el usuario en el gate
      de aprobación): permite usar la CLI en un script o pipeline de CI que
      necesite reaccionar al resultado sin parsear texto.
    - --no-trace es opcional (por defecto SÍ se traza a Langfuse, igual que
      el resto del sistema): existe solo para poder correr la CLI sin
      credenciales de Langfuse configuradas, no porque tracear sea indeseable
      por defecto.
    - grafo.stream(..., stream_mode="updates") en vez de .invoke(): así se ve
      progreso en vivo (qué nodo terminó, en qué momento) en vez de esperar
      en silencio hasta el final. Es también lo que hace visible el gate de
      Human-in-the-Loop (graph/edges.py::request_plan_approval) — ese nodo
      hace un input() real, así que al llegar el stream ahí el proceso se
      pausa en pantalla esperando al usuario, sin lógica extra acá para
      "manejar" la pausa. El estado final se reconstruye a mano acumulando
      las actualizaciones de cada nodo: messages/errors se concatenan (mismo
      reducer operator.add que declara graph/state.py), el resto se
      sobreescribe (mismo criterio: "gana el último en escribir").
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from graph.nodes import NodeExecutionError
from graph.state import create_initial_state
from graph.workflow import MAX_ITERATIONS, build_graph, default_invoke_config
from observability.langfuse_config import flush_traces

load_dotenv()

__all__ = ["main"]

_NODE_LABELS = {
    "product_agent": "Product Agent",
    "architect_agent": "Architect Agent",
    "request_plan_approval": "Aprobación humana del plan",
    "cancelled_by_human": "Pipeline cancelado por el usuario",
    "developer_agent": "Developer Agent",
    "security_agent": "Security Agent",
    "testing_agent": "Testing Agent",
    "reviewer_agent": "Reviewer Agent",
    "advance_iteration": "Actualizando contador de iteración",
    "escalate_to_human": "Escalando a revisión humana",
}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description=(
            "Corre un requerimiento de punta a punta por el pipeline multiagente "
            "(Product -> Architect -> Developer -> Security -> Testing -> Reviewer)."
        ),
    )
    parser.add_argument(
        "requirement",
        nargs="?",
        help="Requerimiento en lenguaje natural. Si se omite, se pide de forma interactiva.",
    )
    parser.add_argument(
        "--no-trace",
        action="store_true",
        help="No enviar trazas a Langfuse para esta corrida.",
    )
    parser.add_argument(
        "--propuesta",
        action="store_true",
        help=(
            "Modo propuesta: Developer/Testing generan diffs y casos de test SIN escribir ni "
            "ejecutar nada en el repo real. Al final se guarda un archivo de propuesta en "
            "propuestas/ (dentro de este workspace)."
        ),
    )
    return parser.parse_args(argv)


_AREA_POR_AGENTE = {
    "product_agent": "Especificación (Product Agent)",
    "architect_agent": "Arquitectura (Architect Agent)",
    "developer_agent": "Implementación (Developer Agent)",
    "security_agent": "Revisión de seguridad (Security Agent)",
    "testing_agent": "Testing (Testing Agent)",
}


def _escribir_propuesta_md(resultado: dict, ruta: Path) -> None:
    """Vuelca el contenido de la propuesta (cambios de código + casos de test
    propuestos + veredicto) a un archivo Markdown. No toca el repo objetivo:
    escribe dentro de este workspace."""
    import datetime

    requirement = resultado.get("requirement", "")
    architecture = resultado.get("architecture", {})
    security_review = resultado.get("security_review", {})
    implementation = resultado.get("implementation", {})
    test_results = resultado.get("test_results", {})
    review = resultado.get("review", {})
    rechazada = review.get("status") == "REJECTED"

    lineas: list[str] = []
    lineas.append("# Propuesta de cambios")
    lineas.append("")
    lineas.append(f"Generada: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if rechazada:
        lineas.append("")
        lineas.append(
            "> ⚠ **Esta propuesta fue RECHAZADA por el Reviewer.** Incluye "
            "correcciones pendientes en las áreas indicadas en la sección "
            "\"Correcciones requeridas\" al final del documento."
        )
    lineas.append("")
    lineas.append("## Requerimiento")
    lineas.append("")
    lineas.append(f"> {requirement}")
    lineas.append("")

    lineas.append("## Arquitectura propuesta")
    lineas.append("")
    lineas.append(f"**Resumen:** {architecture.get('resumen', '(sin resumen)')}")
    if architecture.get("stack"):
        lineas.append("")
        lineas.append("**Stack:** " + ", ".join(architecture["stack"]))
    if architecture.get("componentes"):
        lineas.append("")
        lineas.append("**Componentes:**")
        for c in architecture["componentes"]:
            lineas.append(f"- {c}")
    if architecture.get("plan_alto_nivel"):
        lineas.append("")
        lineas.append("**Plan de alto nivel:**")
        for p in architecture["plan_alto_nivel"]:
            lineas.append(f"- {p}")
    lineas.append("")

    if security_review.get("hallazgos"):
        lineas.append("## Hallazgos de seguridad relevantes")
        lineas.append("")
        for h in security_review["hallazgos"]:
            sev = h.get("severidad", "")
            cat = h.get("categoria_owasp", "")
            desc = h.get("descripcion", "")
            lineas.append(f"- **{sev}** ({cat}): {desc}")
        lineas.append("")

    lineas.append("## Cambios de código propuestos")
    lineas.append("")
    if implementation.get("resumen"):
        lineas.append(f"_{implementation['resumen']}_")
        lineas.append("")
    if implementation.get("archivos_creados") or implementation.get("archivos_modificados"):
        lineas.append(
            f"Archivos a crear: {', '.join(implementation.get('archivos_creados', [])) or 'ninguno'}"
        )
        lineas.append(
            f"Archivos a modificar: {', '.join(implementation.get('archivos_modificados', [])) or 'ninguno'}"
        )
        lineas.append("")
    diff_codigo = implementation.get("diff", "")
    if diff_codigo and diff_codigo.strip():
        lineas.append("### Diffs")
        lineas.append("")
        lineas.append("```diff")
        lineas.append(diff_codigo.rstrip("\n"))
        lineas.append("```")
        lineas.append("")
    else:
        lineas.append("_(sin cambios de código propuestos)_")
        lineas.append("")

    lineas.append("## Casos de test propuestos")
    lineas.append("")
    if test_results.get("resumen"):
        lineas.append(f"_{test_results['resumen']}_")
        lineas.append("")
    diff_tests = test_results.get("diff", "")
    if diff_tests and diff_tests.strip() and test_results.get("propuesta"):
        lineas.append("### Diffs de los casos de test")
        lineas.append("")
        lineas.append("```diff")
        lineas.append(diff_tests.rstrip("\n"))
        lineas.append("```")
        lineas.append("")
    else:
        lineas.append("_(sin casos de test propuestos)_")
        lineas.append("")

    lineas.append("## Veredicto del Reviewer")
    lineas.append("")
    lineas.append(f"**Estado:** {review.get('status', 'DESCONOCIDO')}")
    if review.get("resumen"):
        lineas.append(f"**Resumen:** {review['resumen']}")
    if rechazada:
        area = _AREA_POR_AGENTE.get(
            review.get("return_to"), review.get("return_to") or "(área no especificada)"
        )
        lineas.append("")
        lineas.append("## Correcciones requeridas")
        lineas.append("")
        lineas.append(f"El Reviewer rechazó la propuesta. Aplicar las correcciones en: **{area}**.")
        if review.get("motivos"):
            lineas.append("")
            lineas.append("**Motivos del rechazo:**")
            for m in review["motivos"]:
                lineas.append(f"- {m}")
        if review.get("feedback"):
            lineas.append("")
            lineas.append(f"**Feedback del Reviewer:** {review['feedback']}")
    else:
        if review.get("motivos"):
            lineas.append("")
            lineas.append("**Motivos:**")
            for m in review["motivos"]:
                lineas.append(f"- {m}")
        if review.get("status") == "REJECTED" and review.get("return_to"):
            lineas.append("")
            lineas.append(f"Devuelto a: `{review['return_to']}`")
        if review.get("feedback"):
            lineas.append("")
            lineas.append(f"**Feedback:** {review['feedback']}")
    lineas.append("")

    ruta.write_text("\n".join(lineas), encoding="utf-8")


def _resumen_veredicto(resultado: dict) -> str:
    review = resultado.get("review", {})
    lineas = [
        f"Veredicto: {review.get('status', 'DESCONOCIDO')}",
        f"Resumen: {review.get('resumen', '(sin resumen)')}",
    ]
    if review.get("motivos"):
        lineas.append("Motivos:")
        lineas.extend(f"  - {m}" for m in review["motivos"])
    if review.get("status") == "REJECTED":
        lineas.append(f"Devuelto a: {review.get('return_to')}")
    if review.get("feedback"):
        lineas.append(f"Feedback: {review['feedback']}")
    lineas.append(f"Iteraciones usadas: {resultado.get('iteration', 0)}/{MAX_ITERATIONS}")
    if resultado.get("human_review_required"):
        lineas.append(
            "ALERTA: se agotaron las iteraciones sin llegar a APPROVED — requiere revisión humana."
        )
    return "\n".join(lineas)


def _imprimir_seccion(titulo: str, datos: dict) -> None:
    print(f"\n--- {titulo} ---")
    print(json.dumps(datos, indent=2, ensure_ascii=False))


def _imprimir_mensajes_reviewer(resultado: dict) -> None:
    """Imprime en terminal los mensajes que aportó el Reviewer Agent a la
    bitácora (veredicto + motivos), para que el usuario vea de inmediato por
    qué se aprobó o rechazó la propuesta."""
    mensajes = [
        m
        for m in resultado.get("messages", [])
        if isinstance(m, str) and m.startswith("reviewer_agent:")
    ]
    review = resultado.get("review", {})
    print("\n" + "=" * 70)
    print("Mensajes del Reviewer Agent")
    print("=" * 70)
    if review:
        print(f"Veredicto: {review.get('status', 'DESCONOCIDO')}")
        if review.get("resumen"):
            print(f"Resumen: {review['resumen']}")
        if review.get("motivos"):
            print("Motivos:")
            for m in review["motivos"]:
                print(f"  - {m}")
        if review.get("status") == "REJECTED":
            print(f"Devuelto a: {review.get('return_to')}")
        if review.get("feedback"):
            print(f"Feedback: {review['feedback']}")
    if mensajes:
        print("\nBitácora del Reviewer:")
        for m in mensajes:
            print(f"  {m}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _parse_args(argv)

    requirement = args.requirement or input("Requerimiento: ").strip()
    if not requirement or not requirement.strip():
        print("ERROR - no se proporcionó ningún requerimiento.", file=sys.stderr)
        return 1

    print("=" * 70)
    print("Sistema multiagente — corriendo pipeline completo")
    print("=" * 70)
    print(f"Requerimiento: {requirement}")
    if args.propuesta:
        print("[MODO PROPUESTA] Developer/Testing generarán una propuesta sin tocar el repo real.")
    print("\nCorriendo Product -> Architect -> Developer -> Security -> Testing -> Reviewer...")
    print("(llamadas reales a LLM/RAG/MCP — puede tardar varios minutos)\n")

    grafo = build_graph()
    estado_inicial = create_initial_state(requirement, proposal_mode=args.propuesta)
    config = None if args.no_trace else default_invoke_config()

    resultado: dict = dict(estado_inicial)
    try:
        for evento in grafo.stream(estado_inicial, config=config, stream_mode="updates"):
            for nodo, actualizacion in evento.items():
                etiqueta = _NODE_LABELS.get(nodo, nodo)
                print(f"   >> [{etiqueta}] completado.")
                for clave, valor in actualizacion.items():
                    if clave in ("messages", "errors"):
                        resultado[clave] = resultado.get(clave, []) + valor
                    else:
                        resultado[clave] = valor
    except NodeExecutionError as exc:
        print(f"\nERROR - el pipeline falló en el nodo '{exc.node_name}': {exc.original}", file=sys.stderr)
        return 1
    finally:
        if not args.no_trace:
            flush_traces()

    print("\n" + "=" * 70)
    print("Resultado final")
    print("=" * 70)
    print(_resumen_veredicto(resultado))

    _imprimir_seccion("Especificación (Product Agent)", resultado.get("specification", {}))
    _imprimir_seccion("Arquitectura (Architect Agent)", resultado.get("architecture", {}))
    implementacion = {k: v for k, v in resultado.get("implementation", {}).items() if k != "diff"}
    _imprimir_seccion("Implementación (Developer Agent) — diff omitido, ver 'implementation[\"diff\"]'", implementacion)
    _imprimir_seccion("Seguridad (Security Agent)", resultado.get("security_review", {}))
    _imprimir_seccion("Testing (Testing Agent)", resultado.get("test_results", {}))
    _imprimir_mensajes_reviewer(resultado)

    review = resultado.get("review", {})
    if args.propuesta and review.get("status") != "CANCELLED":
        # Modo propuesta: escribir el archivo de propuesta en propuestas/
        # (dentro de este workspace, sin tocar el repo objetivo). Se omite si
        # el usuario canceló en el gate Human-in-the-Loop (no hay propuesta).
        propuestas_dir = Path(__file__).resolve().parent / "propuestas"
        propuestas_dir.mkdir(exist_ok=True)
        import datetime as _dt

        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        ruta_propuesta = propuestas_dir / f"propuesta_{ts}.md"
        _escribir_propuesta_md(resultado, ruta_propuesta)
        print("\n" + "=" * 70)
        print(f"ARCHIVO DE PROPUESTA GENERADO: {ruta_propuesta}")
        print("=" * 70)

    if review.get("status") == "APPROVED":
        return 0
    if review.get("status") == "CANCELLED":
        return 3
    if resultado.get("human_review_required"):
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
