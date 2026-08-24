# Guía de construcción paso a paso — oscar26-zs-multi-agentic-system

Principio general: **de adentro hacia afuera**. Primero lo que no depende de nada (contratos, entorno), después lo que depende de una sola cosa (RAG, MCP), después los agentes (que dependen de RAG/MCP/estado), y al final lo que depende de todo (el grafo, la UI). Así nunca construyes algo que necesita una pieza que todavía no existe.

---

## Fase 0 — Entorno y decisiones globales (una sola vez, al inicio)

**Por qué va primero:** si decides el proveedor de LLM, el vector store, o instalas dependencias a medias mientras ya escribiste código, vas a tener que volver atrás. Aquí se decide todo de una vez.

**Qué hacer:**
1. Crear el entorno virtual y un único `requirements.txt` con TODO lo que vas a necesitar en todo el proyecto (no lo vayas completando fase por fase — instálalo todo ahora):
   ```
   langchain
   langgraph
   langchain-anthropic      # o langchain-openai, según tu proveedor
   langchain-community
   chromadb                 # vector store local, sin servidor externo — evita otra instalación/config
   langfuse
   python-dotenv
   pydantic
   fastapi
   uvicorn                  # si tu interfaz será API REST; usa streamlit si prefieres eso
   pytest
   mcp                      # SDK oficial de Model Context Protocol
   tiktoken
   ```
2. Decide **ahora** y no cambies después:
   - Proveedor de LLM (ej. Anthropic Claude) — se usa en los 6 agentes.
   - Vector store (Chroma es la opción con menos fricción: corre embebido, sin servidor aparte).
   - Modelo de embeddings.
   - Dónde vive el repo del sistema MVC (ruta local o URL del repo Git) al que apuntará tu MCP.
3. Llena `.env.example` con TODAS las variables que vas a necesitar en cualquier fase (API key del LLM, keys de Langfuse, ruta del repo MVC, etc.), aunque todavía no las uses. Así nunca vuelves a este archivo.

**Resultado de la fase:** entorno instalado una sola vez, cero decisiones pendientes que afecten código futuro.

---

## Fase 1 — `graph/state.py` (el contrato, antes que cualquier agente)

**Por qué va aquí y no después:** cada agente que escribas va a leer y escribir campos de este estado. Si lo defines a medias y luego le agregas campos mientras escribes los agentes, vas a tener que volver a tocar los agentes ya hechos.

**Qué hacer:** definir el `TypedDict` completo con TODOS los campos que vas a necesitar, aunque algunos agentes que los llenan no existan todavía:

```python
class EngineeringState(TypedDict):
    requirement: str
    specification: dict       # output del Product Agent
    architecture: dict        # output del Architecture Agent
    implementation: dict      # output del Developer Agent (incluye el diff)
    security_review: dict     # output del Security Agent
    test_results: dict        # output del Testing Agent
    review: dict               # output del Reviewer Agent
    iteration: int
    messages: list
    human_review_required: bool
```

Piensa bien esta parte ahora, incluso los campos de agentes que construirás en la fase 4 — es mucho más barato agregar un campo aquí en 5 minutos que descubrirlo a medio camino.

---

## Fase 2 — `observability/langfuse_config.py` (esqueleto, antes de los agentes)

**Por qué tan temprano:** si escribes los 6 agentes primero y agregas tracing después, tienes que volver a tocar cada uno para envolverlo. Si el decorador/wrapper de Langfuse existe desde antes, cada agente nace ya instrumentado.

**Qué hacer:** crear una función o decorador reutilizable, por ejemplo:
```python
from langfuse import observe

# luego en cada agente: @observe(name="product_agent")
```
No necesitas que funcione perfecto todavía — solo que exista el decorador para importarlo desde el primer agente que escribas.

---

## Fase 3 — `knowledge/` (contenido real de los documentos)

**Por qué antes del RAG y de los agentes:** el pipeline de RAG (fase 4) necesita documentos reales para poder probarse. Si escribes `ingestion.py` contra carpetas vacías, no vas a saber si funciona hasta después, mezclando errores de contenido con errores de código.

**Qué hacer:** llenar los 7 `.md` que ya tienes con contenido real (no placeholder):
- `architecture/architecture-guidelines.md` y `api-design-guidelines.md`: basados en cómo está armado tu sistema MVC real.
- `security/security-guidelines.md` y `owasp-guidelines.md`: pueden ser un resumen curado de OWASP Top 10 + tus propias políticas.
- `development/coding-standards.md` y `clean-code-guidelines.md`: las convenciones reales de tu repo MVC.
- `testing/testing-strategy.md`: cómo se prueban actualmente los módulos del sistema MVC.

---

## Fase 4 — `rag/` (ingestion, vector_store, retrievers) — probado de forma aislada

**Por qué antes de los agentes:** el Architecture y Security Agent van a llamar a un retriever. Si construyes el retriever y el agente al mismo tiempo, cuando algo falle no sabrás si es el RAG o el agente.

**Qué hacer, en este orden:**
1. `vector_store.py`: función que inicializa Chroma (o el que hayas elegido).
2. `ingestion.py`: script que carga los `.md` de `knowledge/`, los chunkea, genera embeddings y los guarda. **Ejecútalo como script suelto** (`python rag/ingestion.py`) y confirma que se crean los vectores — no sigas hasta que esto funcione solo.
3. `retrievers.py`: uno por dominio (architecture retriever, security retriever, testing retriever) — no un retriever único, como recomienda el enunciado. **Pruébalo aislado**: pide "reglas de idempotencia" y confirma que devuelve fragmentos coherentes, sin ningún agente todavía en el medio.

**No avances a la fase 6 (agentes) hasta que esta fase funcione de forma standalone.**

---

## Fase 5 — `mcp/server.py` — probado de forma aislada contra tu repo MVC real

**Por qué antes del Developer Agent:** mismo motivo que el RAG — si construyes el MCP y el agente que lo usa al mismo tiempo, no vas a poder aislar errores.

**Qué hacer:**
1. Define las tools mínimas: `list_files()`, `read_file()`, `search_code()`, y si vas a permitir escritura: `update_file()`, `get_diff()`. Estas apuntan a la ruta/repo de tu sistema MVC (idealmente una copia local o una rama de prueba, no el repo de producción).
2. Corre el servidor MCP de forma aislada y pruébalo con un cliente MCP simple (o incluso llamando las funciones directamente en un script), **sin ningún agente todavía**. Confirma que `read_file("algún_controlador.py")` te devuelve contenido real de tu MVC.

**No avances a construir el Developer Agent hasta que esto responda correctamente.**

---

## Fase 6 — `agents/` — uno a la vez, del más simple al más dependiente, cada uno probado solo

Este es el orden que menos retrocesos genera, porque vas agregando una dependencia nueva a la vez:

| Orden | Agente | Nueva dependencia que introduce | Cómo probarlo aislado |
|---|---|---|---|
| 1 | `product_agent.py` | Ninguna (solo LLM + structured output) | Llamarlo con un string de requerimiento y verificar que devuelve el JSON estructurado |
| 2 | `architect_agent.py` | RAG (fase 4, ya lista) | Pasarle una `specification` de prueba y confirmar que cita el retriever de architecture |
| 3 | `security_agent.py` | RAG de seguridad (ya lista) | Pasarle una `architecture` de prueba, sin MCP todavía si no lo necesitas para el análisis estático |
| 4 | `developer_agent.py` | MCP (fase 5, ya lista) | Pasarle una `architecture` de prueba y confirmar que hace `read_file`/`update_file` reales sobre tu MVC |
| 5 | `testing_agent.py` | MCP `run_tests` (agrégalo al `mcp/server.py` si no lo tenías) | Confirmar que corre pruebas reales y devuelve pass/fail |
| 6 | `reviewer_agent.py` | Ninguna nueva (solo lee el resto del estado) | Pasarle un estado completo simulado a mano y confirmar que devuelve `APPROVED`/`REJECTED` con `return_to` |

**Regla de oro de esta fase:** cada agente se prueba con un script suelto (`if __name__ == "__main__":` o un test rápido) ANTES de tocar el grafo. Si un agente falla dentro del grafo más adelante, ya sabrás que el problema es de conexión, no del agente en sí.

---

## Fase 7 — `graph/nodes.py`, `edges.py`, `workflow.py` — primero el camino feliz, sin ciclos

**Por qué esperar hasta aquí:** conectar agentes que no funcionan individualmente es la fuente #1 de retrocesos. Con la fase 6 completa, aquí solo estás cableando piezas ya probadas.

**Qué hacer, en este orden:**
1. `nodes.py`: envolver cada agente como nodo de LangGraph (llamando a la función que ya probaste).
2. `workflow.py`: construir el grafo lineal más simple posible — Product → Architecture → Developer → Security → Testing → Reviewer → END, **sin ciclos todavía**. Correr un requerimiento de punta a punta y confirmar que el estado fluye correctamente entre nodos.
3. Solo cuando el camino feliz funcione, agregar `edges.py` con el conditional edge: si Reviewer rechaza, volver al agente indicado (`return_to`), con el contador `iteration` y el límite `MAX_ITERATIONS = 3`.

---

## Fase 8 — `tests/` — escenarios reales, incluyendo los que deben fallar

**Por qué al final:** necesitas el grafo completo funcionando para poder correr escenarios de punta a punta.

**Qué hacer:**
- `test_agents.py`: tests unitarios de cada agente (reutilizando lo que ya probaste sueltos en la fase 6).
- `test_graph.py`: tests de que el routing condicional funciona (ej. forzar un `REJECTED` y confirmar que vuelve al agente correcto).
- `test_scenarios.py`: tus 5+ escenarios completos, incluyendo los 2 diseñados para fallar (IDOR, race condition, etc.).

---

## Fase 9 — `app.py` — la interfaz, al final, es solo una capa delgada

**Por qué al final:** `app.py` no hace nada nuevo, solo expone `workflow.py` (CLI, FastAPI o Streamlit). Construirlo antes sería exponer algo que todavía no existe.

---

## Fase 10 — `README.md` y diagramas — lo último, con todo ya probado

Documenta con datos reales de tus propias ejecuciones (capturas de Langfuse, ejemplos de trace, métricas), no con descripciones especulativas de lo que "debería" pasar.

---

## Resumen del orden (para pegar en tu checklist)

1. Entorno + `.env` + requirements completos
2. `graph/state.py`
3. `observability/langfuse_config.py` (esqueleto)
4. `knowledge/*.md` (contenido real)
5. `rag/` (probado aislado)
6. `mcp/server.py` (probado aislado contra tu MVC)
7. `agents/` uno por uno: product → architect → security → developer → testing → reviewer
8. `graph/` (camino feliz primero, luego ciclos)
9. `tests/`
10. `app.py`
11. `README.md` y diagramas