# autonomous-swe-team

Sistema multiagente que simula un equipo de desarrollo de software autónomo
(producto, arquitectura, desarrollo, seguridad, testing y revisión),
orquestado con LangGraph, con RAG sobre guías internas, tools MCP para operar
sobre un repositorio de ejemplo y trazabilidad con Langfuse.

## Estado del proyecto

Estructura inicial (scaffold). Todos los archivos son placeholders: cada uno
incluye una nota explicando su responsabilidad dentro del sistema y qué se
espera que contenga al implementarse. Todavía no hay lógica funcional.

## Arquitectura

Flujo principal del grafo:

```
Product Agent -> Architect Agent -> Developer Agent -> Security Agent -> Testing Agent -> Reviewer Agent
```

- **Ciclo de revisión:** el Reviewer emite `APPROVED` (fin del flujo) o
  `REJECTED` (retorna el trabajo al agente indicado en `return_to`), con un
  límite configurable `MAX_ITERATIONS`.
- **Estado compartido:** `EngineeringState` (`graph/state.py`) viaja por
  todos los nodos como única fuente de verdad.
- **RAG:** los documentos de `knowledge/` se ingesta a un vector store
  (`rag/`) y se exponen como retrievers especializados por dominio.
- **MCP:** servidor propio (`mcp/server.py`) con tools `read_file`,
  `list_files`, `search_code`, `create_file`, `get_diff`, `run_tests`.
- **Observabilidad:** Langfuse (`observability/langfuse_config.py`) registra
  prompts, tokens, latencia y errores por agente.

## Estructura

```
autonomous-swe-team/
├── agents/          # Los 6 agentes especializados
├── graph/           # Estado, nodos, edges y workflow de LangGraph
├── rag/             # Ingesta, retrievers y vector store
├── mcp/             # Servidor MCP propio (tools sobre el repo)
├── observability/   # Configuración de Langfuse
├── knowledge/       # Guías internas (arquitectura, seguridad, desarrollo, testing)
├── tests/           # Pruebas unitarias, del grafo y escenarios e2e
├── app.py           # Punto de entrada
├── requirements.txt # Dependencias (vacío por ahora)
├── .env.example     # Variables de entorno requeridas
└── README.md
```

## Tecnologías previstas

Python 3.10+, LangGraph, LangChain, proveedor LLM (OpenAI/Anthropic), Chroma o
FAISS, MCP, Langfuse y pytest.

## Instalación

1. Clonar el repositorio y entrar a la carpeta del proyecto.
2. Crear entorno virtual: `python -m venv .venv`
3. Activarlo: `.venv\Scripts\activate` (Windows) o `source .venv/bin/activate`.
4. Instalar dependencias: `pip install -r requirements.txt` (vacío por ahora).

## Configuración

Copiar `.env.example` a `.env` y completar las variables (API keys del LLM,
credenciales de Langfuse y configuración del vector store). Nunca commitear
el archivo `.env`.

## Ejecución

```bash
python app.py
```

(Interfaz por definir: CLI / Streamlit / API según la implementación.)

## Pruebas

```bash
pytest tests/
```
