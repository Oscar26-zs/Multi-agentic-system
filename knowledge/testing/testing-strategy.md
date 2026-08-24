# Estrategia de Pruebas — Sistema de Solicitudes de Vacaciones

Basado en `constitution.md` sección 9 y el plan de pruebas por capa de `plan.md`.

## Pirámide de pruebas (obligatoria, 3 niveles)

| Nivel | Tecnología | Qué cubre | Dependencias externas |
|---|---|---|---|
| **Unitarias** | xUnit + Moq / NSubstitute | Reglas de dominio, servicios de aplicación, validadores | Ninguna — Domain se testea sin mocks (es lógica pura); Application se testea con mocks de repositorios |
| **Integración** | xUnit + WebApplicationFactory | Repositorios contra BD real (SQLite o SQL Server), controladores, middleware, pipeline completo de una request | BD real (LocalDB/Testcontainers) |
| **End-to-End (E2E)** | Playwright | Flujos críticos completos: crear solicitud, aprobar, rechazar, cancelar. Cubren HU-01 a HU-09 | Aplicación completa levantada |

## Proyectos de test, uno por capa (mapeo directo a la estructura de producción)

- `Vacations.Domain.Tests` — **unitarias puras, sin mocks**. Ejemplo: dado un `RangoFechas` con fecha de inicio un sábado, verificar que `CalcularDiasHabiles()` excluye correctamente sábados y domingos pero cuenta feriados.
- `Vacations.Application.Tests` — **unitarias con mocks** de `IRepositorioSolicitudVacaciones`, `IRepositorioSaldoEmpleado` y `TimeProvider`. Ejemplo: `CrearSolicitudCommand` con un repositorio mockeado que reporta solapamiento debe lanzar `TraslapeSolicitudesException`, no crear la solicitud.
- `Vacations.Infrastructure.Tests` — **integración contra BD real** (SQL Server LocalDB o Testcontainers). Verifica que las `Configurations` de EF Core, el interceptor de auditoría y `RowVersion` funcionan tal como se diseñaron, no solo en memoria.
- `Vacations.Web.Tests` — **integración de sistema** con `WebApplicationFactory`. Verifica el pipeline HTTP completo: autorización por rol, anti-forgery, binding de ViewModels, respuesta ante reglas de negocio violadas.

## Meta de cobertura

- **Mínimo 80%** en `Domain/` y `Application/` — no negociable, es gate de CI.
- Cobertura en Infrastructure y Presentation: deseable, sin mínimo exigido.
- La cobertura porcentual **no reemplaza** la obligación de probar cada invariante universal (ver abajo) y cada criterio de aceptación de las historias de usuario — un 80% de cobertura que no toca los invariantes críticos no cumple el estándar del proyecto.

## Invariantes universales que TODA prueba de regresión debe cubrir

Estos 9 invariantes (constitution.md sección 7) son independientes de política de negocio y deben tener test dedicado:

1. Saldo disponible nunca negativo — test de aprobación concurrente que casi lo deja negativo.
2. Fecha de inicio ≤ fecha de fin.
3. Sin fechas pasadas en flujo normal de creación.
4. Toda solicitud nueva nace en estado `Pending`.
5. Solo las transiciones documentadas de la máquina de estados son válidas — test negativo por cada transición no permitida (ej. `Rejected → Approved` debe fallar).
6. Estados finales inmutables, salvo `Approved → Cancelled` antes del inicio del periodo.
7. Prohibición de auto-aprobación (`aprobador == autor`).
8. Toda transición de estado genera registro de auditoría inmutable.
9. El cálculo de días solicitados ocurre siempre en servidor — test que envía un valor de días manipulado desde el "cliente" y confirma que el servidor lo ignora/recalcula.

## Casos de abuso — pruebas de seguridad obligatorias antes de cada release

Ver `security-guidelines.md` para el detalle; deben existir como tests explícitos, no solo como revisión manual:
- Acceso cruzado entre empleados (Empleado A ↔ solicitudes de Empleado B).
- IDOR vía manipulación de parámetros de ruta/query.
- Forced browsing entre roles (empleado accede a rutas de aprobador y viceversa).
- Escalación de privilegios.
- Auto-aprobación bloqueada.
- Fallos de CSRF (POST sin anti-forgery token).
- Transiciones duplicadas (doble aprobación sobre la misma solicitud).
- Envíos duplicados (doble creación por concurrencia).

## Escenarios de prueba end-to-end obligatorios (trazables a Historias de Usuario)

Cada HU-01 a HU-09 de `spec.md` debe tener al menos un escenario E2E con Playwright que verifique sus criterios de aceptación literales (incluye los mensajes de error exactos, no solo el código de estado HTTP). Ejemplos concretos de escenarios "deben fallar" que el Testing Agent debe generar, no solo los de camino feliz:

- Crear solicitud con fecha de inicio = hoy → debe bloquear con `"La fecha de inicio no puede ser anterior a mañana"`.
- Crear solicitud que excede el saldo disponible → debe bloquear con `"Saldo insuficiente para esta solicitud"`.
- Crear solicitud que se solapa con una `Pending` existente del mismo empleado → debe bloquear la creación (no solo advertir).
- Aprobador intenta aprobar su propia solicitud → bloqueado, no debe aparecer en su propia bandeja.
- Aprobador inactivo intenta aprobar → bloqueado.
- Intentar cancelar una solicitud `Approved` cuyo periodo ya inició → bloqueado con `"No se puede cancelar: el periodo de vacaciones ya ha iniciado"`.
- Solicitud `Pending` cuya fecha de inicio se alcanza sin resolución → debe auto-expirar a `Expired` con actor `SISTEMA_AUTO_EXPIRACION`.
- Rechazo sin comentario → debe bloquearse (comentario obligatorio al rechazar, máx. 500 caracteres).
- RRHH intenta invocar una acción de aprobación directamente (forced browsing) → 403, aunque la UI no muestre el botón.

## Determinismo en pruebas dependientes del tiempo

Como Domain y Application usan `TimeProvider` inyectado (nunca `DateTime.Now` directo), las pruebas de reglas dependientes de fecha (antelación mínima de 1 día, horizonte máximo de 2 meses, auto-expiración) deben usar un `TimeProvider` fake/fijo, no depender del reloj real de la máquina de test — de lo contrario el test es no determinista y puede fallar en CI según la hora de ejecución.

## Gate de CI obligatorio antes de mergear a `main`

| Gate | Herramienta |
|---|---|
| Build | `dotnet build` sin errores ni warnings |
| Formato | `dotnet format --verify-no-changes` |
| Analizadores estáticos | .NET Roslyn analyzers + SonarCloud (si disponible) |
| Pruebas | `dotnet test` — todas deben pasar |
| Cobertura | `dotnet test --collect:"XPlat Code Coverage"` — mínimo 80% en Domain/Application |
| Escaneo de dependencias | `dotnet list package --vulnerable` — cero vulnerabilidades conocidas |
| Validación de diagramas | Los `.md` con diagramas Mermaid no deben tener cambios no reflejados respecto a `main` |

Si cualquier gate falla, la fusión se bloquea — no hay excepción silenciosa.
