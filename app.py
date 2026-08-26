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
      sin escalar, 2=escalado a humano): permite usar la CLI en un script o
      pipeline de CI que necesite reaccionar al resultado sin parsear texto.
    - --no-trace es opcional (por defecto SÍ se traza a Langfuse, igual que
      el resto del sistema): existe solo para poder correr la CLI sin
      credenciales de Langfuse configuradas, no porque tracear sea indeseable
      por defecto.
"""

import argparse
import json
import sys

from dotenv import load_dotenv

from graph.nodes import NodeExecutionError
from graph.state import create_initial_state
from graph.workflow import MAX_ITERATIONS, build_graph, default_invoke_config
from observability.langfuse_config import flush_traces

load_dotenv()

__all__ = ["main"]


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
    return parser.parse_args(argv)


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
    print("\nCorriendo Product -> Architect -> Developer -> Security -> Testing -> Reviewer...")
    print("(llamadas reales a LLM/RAG/MCP — puede tardar varios minutos)\n")

    grafo = build_graph()
    estado_inicial = create_initial_state(requirement)
    config = None if args.no_trace else default_invoke_config()

    try:
        resultado = grafo.invoke(estado_inicial, config=config)
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

    review = resultado.get("review", {})
    if review.get("status") == "APPROVED":
        return 0
    if resultado.get("human_review_required"):
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
