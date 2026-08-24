# Estándares de Código — Sistema de Solicitudes de Vacaciones

Convenciones reales del proyecto MVC (`constitution.md` sección 4), en C# / ASP.NET Core / EF Core.

## Nomenclatura (regla no negociable: español, PascalCase)

| Elemento | Convención | Ejemplo real |
|---|---|---|
| Entidades de Dominio | Español, PascalCase | `SolicitudVacaciones`, `SaldoEmpleado`, `Empleado`, `HistorialSolicitud` |
| Clases de Application | Español, PascalCase, sufijo por tipo | `CrearSolicitudCommand`, `AprobarSolicitudCommand`, `ObtenerSaldoQuery`, `CrearSolicitudHandler` |
| Clases de Infrastructure | Español, PascalCase | `VacacionesDbContext`, `RepositorioSolicitudVacaciones`, `ProveedorTiempoSistema` |
| Controladores | Español, sufijo `Controller` | `SolicitudVacacionesController`, `BandejaAprobadorController` |
| Vistas | Español, carpeta = nombre del controlador | `Views/SolicitudVacaciones/`, `Views/BandejaAprobador/` |
| Propiedades y métodos públicos | Español, PascalCase | `FechaInicio`, `CalcularDiasHabiles()` |
| Campos privados | `_camelCase` en español | `_repositorioSolicitud` |
| Parámetros y variables locales | `camelCase` en español | `fechaInicio`, `diasSolicitados` |
| Tablas de base de datos | Español, PascalCase, singular | `SolicitudVacaciones` (tabla), no `SolicitudesVacaciones` |
| Columnas de base de datos | Español, PascalCase | `SolicitudVacaciones.FechaInicio` |
| Rutas/endpoints | Español, kebab-case | `/solicitudes-vacaciones/pendientes` |
| Mensajes de validación y UI | Español, texto exacto de la especificación | No parafrasear — usar el string literal de `spec.md` |

Regla práctica: si una clase, propiedad o ruta nueva usa una palabra en inglés fuera de términos técnicos estándar (`Id`, `Dto`, `Command`, `Query`, `Handler`), es una desviación del estándar del proyecto.

## Estructura de paquetes/proyectos

4 proyectos .NET independientes, uno por capa (`Vacations.Domain`, `Vacations.Application`, `Vacations.Infrastructure`, `Vacations.Web`), más 4 proyectos de test paralelos (`Vacations.Domain.Tests`, `Vacations.Application.Tests`, `Vacations.Infrastructure.Tests`, `Vacations.Web.Tests`). Cada proyecto de código de producción referencia solo las capas permitidas por la dirección de dependencia (ver `architecture-guidelines.md`).

Dentro de cada capa:
- `Domain/Entities/`, `Domain/Enums/`, `Domain/ValueObjects/`, `Domain/Exceptions/`, `Domain/Abstractions/` (interfaces de repositorio).
- `Application/<Área>/Commands/`, `Application/<Área>/Queries/` — agrupado por área funcional (`Solicitudes/`, `Saldos/`, `Expiracion/`), no por tipo técnico plano.
- `Infrastructure/Persistence/{Configurations,Repositories,Interceptors}/`, `Infrastructure/Identity/`, `Infrastructure/Time/`, `Infrastructure/BackgroundServices/`.
- `Web/Controllers/`, `Web/ViewModels/`, `Web/Views/`, `Web/Authorization/`.

## Tipado

- `Nullable` habilitado (`<Nullable>enable</Nullable>` en el `.csproj`) — toda referencia nullable debe anotarse explícitamente (`string? comentario`).
- `ImplicitUsings` habilitado.
- Campos opcionales de negocio (ej. `approverComment` solo existe si la solicitud fue rechazada) se modelan como nullable, no con valores centinela (`""`, `-1`).
- IDs de entidad: `Guid`, no enteros autoincrementales.
- Enums para valores cerrados de dominio: `EstadoSolicitud` (`Pending`, `Approved`, `Rejected`, `Cancelled`, `Expired`), `RolUsuario` (`Empleado`, `Aprobador`, `RRHH`) — nunca strings mágicos para representar estado.

## Manejo de errores

- Las reglas de negocio violadas se expresan como **excepciones de dominio tipadas y específicas**, no excepciones genéricas: `SaldoInsuficienteException`, `TraslapeSolicitudesException`, `AutoAprobacionNoPermitidaException`, `TransicionEstadoInvalidaException`. Cada una vive en `Domain/Exceptions/`.
- La capa Web traduce estas excepciones a la respuesta HTTP/vista apropiada (mensaje de validación en el formulario, 403, etc.) — no las deja propagar como error 500 genérico.
- `DbUpdateConcurrencyException` (de EF Core) se maneja explícitamente en los handlers de Application que tocan `SolicitudVacaciones` o `SaldoEmpleado`, nunca se deja sin capturar.

## Validación de entrada vs. reglas de negocio (separación estricta)

- **FluentValidation** (aprobado como dependencia estándar) se usa exclusivamente para validación de entrada: formato, campos requeridos, longitud, estructura de ViewModels/Commands. Se resuelve vía DI y se ejecuta explícitamente con `ValidateAsync` desde el caso de uso o un filtro de acción propio.
- **Prohibido** usar el pipeline de auto-validación de MVC (`AddFluentValidationAutoValidation`) o la integración cliente `FluentValidation.AspNetCore` (deprecada) — la ejecución debe ser explícita para no interferir con la validación de negocio del Dominio.
- Las reglas de negocio (saldo, solapamiento, transición de estado, autoridad del actor) **nunca** se implementan con FluentValidation — van en el Dominio como métodos o invariantes de la entidad/value object.

## Abstracción de tiempo (regla estricta)

Prohibido `DateTime.Now` y `DateTime.UtcNow` directo en `Domain` y `Application`. Toda referencia a "ahora" pasa por `TimeProvider` (nativo de .NET, inyectado), implementado en Infrastructure como `ProveedorTiempoSistema`. Esto permite testear reglas dependientes de fecha (auto-expiración, antelación mínima, horizonte máximo) de forma determinista, sin mockear el reloj del sistema operativo.

## Formatters y linters obligatorios (gate de CI)

- `dotnet format --verify-no-changes` — el código debe estar formateado antes de mergear.
- .NET Roslyn analyzers (+ SonarCloud si está disponible).
- `dotnet build` sin errores **ni warnings**.
- `dotnet list package --vulnerable` — cero vulnerabilidades conocidas en dependencias.

## Value Objects como lógica pura

Cálculos de negocio que no requieren estado persistente ni dependencias externas se modelan como Value Objects con lógica pura en Domain — ejemplo real: `RangoFechas` (valida inicio ≤ fin, inicio ≥ mañana, fin ≤ inicio + 2 meses) y el cálculo de `DiasHabiles` (excluye sábados y domingos). Estos no dependen de EF Core ni de ningún servicio externo — son funciones puras testeables sin mocks.
