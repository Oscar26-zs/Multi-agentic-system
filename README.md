# autonomous-swe-team

Sistema multiagente que simula un equipo de desarrollo de software autónomo (producto, arquitectura, desarrollo, seguridad, testing y revisión), orquestado con LangGraph, con RAG sobre guías internas, tools MCP propias para operar sobre un repositorio real y trazabilidad completa con Langfuse.

## Estado del proyecto

Las 10 fases de `Guia_Construccion.md` están completas: los 6 agentes funcionan con LLM real, el grafo corre el camino feliz, el ciclo de revisión y un gate de Human-in-the-Loop antes de tocar el repo real, hay tres capas de tests (unitarios mockeados, estructurales del grafo, y escenarios end-to-end reales), y `app.py` expone todo por CLI con progreso en vivo. Ver [`agents/AGENTS.md`](agents/AGENTS.md) (cómo funciona cada agente, con analogías), [`tests/TESTS.md`](tests/TESTS.md) (estrategia de pruebas) y [`graph/HUMAN_IN_THE_LOOP.md`](graph/HUMAN_IN_THE_LOOP.md) (el gate de aprobación humana) para el detalle.

## Arquitectura

Flujo principal del grafo (`graph/workflow.py`):

```
Product Agent → Architect Agent → [Aprobación humana] → Developer Agent → Security Agent → Testing Agent → Reviewer Agent
                                          │                                                                       │
                                    RECHAZADO                                                     APPROVED ───────┤──→ END
                                          │                                                        REJECTED ──────┘
                                          ▼                                            (vuelve a return_to,
                                        END (CANCELLED)                                 hasta MAX_ITERATIONS=3,
                                                                                         luego escala a humano)
```

- **Estado compartido:** `EngineeringState` (`graph/state.py`) — un diccionario que viaja por todos los nodos; cada agente lee lo que dejaron los anteriores y escribe solo su parte (`specification`, `architecture`, `implementation`, `security_review`, `test_results`, `review`, `plan_approval`).
- **Human-in-the-Loop antes de tocar el repo real:** entre `architect_agent` y `developer_agent`, `graph/edges.py::request_plan_approval` muestra el plan propuesto y pausa esperando aprobación por teclado — `developer_agent` es el único nodo que escribe archivos reales vía MCP, y no corre sin luz verde humana. Si se rechaza, el pipeline termina con `review.status = "CANCELLED"` sin tocar el repo. Ver `graph/HUMAN_IN_THE_LOOP.md`.
- **Ciclo de revisión:** el Reviewer emite `APPROVED` (fin) o `REJECTED` con un `return_to` (a qué agente vuelve el trabajo). Dos guardrails deterministas (no dejados al criterio del LLM) fuerzan `REJECTED` si `security_review["aprobado"]` o `test_results["aprobado"]` son `False`, sin importar qué haya dicho el modelo — ver `agents/reviewer_agent.py::_coerce_verdict`.
- **RAG:** `knowledge/*.md` (arquitectura, seguridad, desarrollo, testing) se ingesta a Chroma (`rag/ingestion.py`) y se expone como retrievers especializados por dominio (`rag/retrievers.py`) — búsquedas locales, sin costo de API.
- **MCP:** servidor propio (`mcp_server/server.py`) con las tools `list_files`, `read_file`, `search_code`, `create_file`, `update_file`, `run_tests` (corre `dotnet test` real vía subprocess), sandboxeadas a `REPO_TARGET_PATH`. `developer_agent.py` y `testing_agent.py` lo consumen por protocolo real (`stdio_client` + `ClientSession`), no por import directo.
- **LLM con fallback multi-proveedor:** `agents/llm_factory.py` prueba OpenRouter → NVIDIA NIM → Groq → Google AI Studio, en ese orden (OpenRouter al frente porque en uso real resultó más rápido/confiable que NVIDIA), hasta que uno responda — tanto para llamadas simples (`build_llm`) como para salida estructurada (`invoke_structured`, que también reintenta si un proveedor conecta pero falla al generar el schema). Evita que el sistema quede bloqueado por la cuota gratuita diaria de un solo proveedor.
- **Observabilidad:** Langfuse (`observability/langfuse_config.py`) traza cada agente (`@observe`) y cada llamada LLM interna (`get_callback_handler`), correlacionadas bajo la misma corrida cuando se invoca vía `graph/workflow.py`.

## Ejemplo real (Product Agent, NVIDIA NIM)

Salida real de `python agents/product_agent.py` con el requerimiento *"Como empleado quiero poder solicitar vacaciones indicando fecha de inicio y fin, y que mi jefe directo apruebe o rechace la solicitud."*:

```json
{
  "resumen": "Un empleado puede solicitar vacaciones especificando fecha de inicio y fin, y su jefe directo puede aprobar o rechazar la solicitud.",
  "actores": ["Empleado", "Jefe directo", "Sistema de gestión de vacaciones"],
  "reglas_negocio": [
    "El empleado debe proporcionar fecha de inicio y fecha de fin, ambas deben ser fechas futuras...",
    "El sistema no permite solapamientos entre solicitudes de vacaciones del mismo empleado..."
  ],
  "riesgos": [
    "Posible autoaprobación si el sistema no verifica correctamente la relación empleado-jefe...",
    "Condiciones de carrera: dos solicitudes simultáneas para el mismo rango de fechas..."
  ],
  "supuestos": ["Cada empleado tiene un registro de 'jefe directo' asignado y actualizado en el sistema de RRHH."]
}
```

(Recortado — ver el JSON completo corriendo el comando vos mismo.) El `security_agent.py` sobre esta misma especificación detectó, entre otros, un hallazgo real de **A01:2021 - Control de Acceso Roto** por falta de validación de propiedad del recurso en el endpoint de aprobación, y un **CSRF** por falta de token anti-forgery — ambos con severidad `alta` y `aprobado=False`.

## Estructura

```
autonomous-swe-team/
├── agents/            # Los 6 agentes + llm_factory.py (fallback multi-proveedor) + mcp_tools.py
│   └── AGENTS.md       # Cómo funciona cada agente, con analogías
├── graph/              # state.py, nodes.py, edges.py, workflow.py (LangGraph)
│   └── HUMAN_IN_THE_LOOP.md  # El gate de aprobación humana, con analogía
├── rag/                # Ingesta, retrievers y vector store (Chroma)
├── mcp_server/          # Servidor MCP propio (tools sobre el repo objetivo)
├── observability/      # Configuración de Langfuse
├── knowledge/           # Guías internas (arquitectura, seguridad, desarrollo, testing)
├── tests/               # test_agents.py, test_graph.py, test_scenarios.py, test_mcp_*.py
│   └── TESTS.md         # Estrategia de pruebas en 3 capas, con analogías
├── app.py               # CLI (punto de entrada)
├── requirements.txt
├── .env.example         # Variables de entorno (con el orden de fallback documentado)
├── pytest.ini            # Excluye tests/test_scenarios.py (@integration) por default
└── _sandbox/             # Repo MVC objetivo clonado aquí (no versionado)
```

## Instalación

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

(En PowerShell, si `Activate.ps1` falla por política de ejecución, usa `.venv\Scripts\python.exe` directo en cada comando, o corre una vez `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.)

## Configuración

1. Copiar `.env.example` a `.env`.
2. Completar **al menos una** de las 4 keys de proveedor LLM (`NVIDIA_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`) — no hace falta elegir una sola, el sistema hace fallback automático entre las que estén configuradas.
3. Clonar el repo MVC objetivo en `REPO_TARGET_PATH` (por defecto `./_sandbox/Solicitud_de_Vacaciones`) — necesario para `developer_agent`, `testing_agent` y `test_scenarios.py`.
4. (Opcional) Completar `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` para trazabilidad en [cloud.langfuse.com](https://cloud.langfuse.com).
5. Poblar el vector store: `.venv\Scripts\python.exe rag\ingestion.py`.

Nunca commitear `.env` (ya está en `.gitignore`).

## Ejecución

```powershell
.venv\Scripts\python.exe app.py "Como empleado quiero poder solicitar vacaciones indicando fecha de inicio y fin, y que mi jefe directo apruebe o rechace la solicitud."
```

Corre el pipeline completo (llamadas reales a LLM/RAG/MCP — tarda varios minutos), imprimiendo progreso en vivo (qué agente terminó, en qué momento) en vez de esperar en silencio hasta el final. Apenas termina `architect_agent`, **pausa y pide aprobación por teclado** antes de dejar avanzar a `developer_agent` (ver `graph/HUMAN_IN_THE_LOOP.md`). Al final imprime el veredicto y el expediente de cada etapa. Código de salida: `0` = `APPROVED`, `1` = `REJECTED`, `2` = escalado a revisión humana (`MAX_ITERATIONS` agotado), `3` = cancelado por el usuario en el gate de aprobación.

También se puede correr cada agente o cada módulo por separado, de forma aislada (mismo patrón en todos: `if __name__ == "__main__":`):

```powershell
.venv\Scripts\python.exe agents\product_agent.py
.venv\Scripts\python.exe graph\workflow.py
```

## Pruebas

```powershell
.venv\Scripts\python.exe -m pytest tests/          # unitarios + grafo, ~70 tests, sin LLM, segundos
.venv\Scripts\python.exe -m pytest tests/ -m integration   # + 5 escenarios reales de punta a punta (cuesta cuota de API, tarda minutos)
```

Ver [`tests/TESTS.md`](tests/TESTS.md) para el porqué de esta separación en 3 capas (unitarios mockeados / estructurales del grafo / end-to-end reales).

## Tecnologías

Python 3.14, LangGraph, LangChain (`langchain-openai` contra proveedores OpenAI-compatibles), Chroma (embeddings ONNX locales, sin GPU), MCP (SDK oficial), Langfuse, pytest.
