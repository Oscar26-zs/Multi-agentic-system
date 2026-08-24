# Guía de Arquitectura — Sistema de Solicitudes de Vacaciones

Basado en la Constitución Técnica del proyecto MVC real (`_sandbox/Solicitud_de_Vacaciones`), un monolito modular en ASP.NET Core MVC con Clean Architecture.

## Estilo arquitectónico

**Monolito modular en 4 capas**, no microservicios (descartados por prematuros para el tamaño del MVP: 3 roles, ~500 empleados, 50-100 usuarios concurrentes). Las capas y su dirección de dependencia:

```
Presentation (Web) → Application → Domain ← Infrastructure
```

- **`Domain`**: entidades, value objects, enums, excepciones de dominio, interfaces de repositorios. CERO dependencias externas — sin ASP.NET Core, sin EF Core, sin ningún framework.
- **`Application`**: casos de uso (commands/queries), orquesta Domain e invoca repositorios vía interfaces. Depende solo de Domain.
- **`Infrastructure`**: implementa las interfaces de Domain/Application (repositorios EF Core, `TimeProvider`, Identity, background services). Depende de Application y Domain.
- **`Web`**: controladores MVC delgados, ViewModels, vistas Razor. Depende solo de Application.

Regla de verificación: si un archivo en `Domain/` o `Application/` importa algo de `Microsoft.AspNetCore.*` o `Microsoft.EntityFrameworkCore`, es una violación de arquitectura — debe rechazarse en review.

## Organización en capas: qué va dónde

| Necesito... | Va en |
|---|---|
| Una regla de negocio (saldo, solapamiento de fechas, transición de estado, anti-auto-aprobación) | `Domain` — nunca en un validador de entrada ni en el controlador |
| Validación de formato/campo requerido/longitud de un formulario | `Application` o `Web`, con FluentValidation |
| Un nuevo tipo de solicitud a la base de datos | `Infrastructure/Persistence/Repositories`, implementando una interfaz ya definida en `Domain` |
| Orquestar "cuando se aprueba, descontar saldo y registrar auditoría" | `Application` (un Command/Handler), nunca en el controlador |
| Lógica de fecha/hora | Vía `TimeProvider` inyectado — **prohibido `DateTime.Now`/`DateTime.UtcNow` directo en Domain/Application** |

## Controladores delgados

Los controladores MVC solo orquestan HTTP: reciben el request, arman el Command/Query, lo despachan a Application, y devuelven la vista o el resultado. No contienen lógica de negocio ni de dominio. Un controlador que calcula días hábiles, valida saldo, o decide una transición de estado está mal ubicado — esa lógica pertenece a Domain.

## Patrones de integración

- **CQRS ligero**: cada caso de uso de negocio se modela como un Command (escritura) o Query (lectura) en `Application`, con handler dedicado. No hay bus de mensajes ni mediator externo obligatorio — puede resolverse con inyección de dependencias directa.
- **Repository pattern**: las interfaces de repositorio (`IRepositorioSolicitudVacaciones`, `IRepositorioSaldoEmpleado`) se definen en `Domain`; la implementación EF Core vive en `Infrastructure`.
- **Interceptor de auditoría**: los cambios de estado y ediciones se registran automáticamente vía un interceptor de `SaveChangesAsync` de EF Core (`InterceptorAuditoriaSaveChanges`), no mediante llamadas manuales dispersas en cada handler — evita el riesgo de olvidar registrar auditoría.
- **Concurrencia optimista**: entidades con estado mutable compartido (`SolicitudVacaciones`, `SaldoEmpleado`) usan `RowVersion` para evitar condiciones de carrera (ej. doble aprobación simultánea, saldo negativo por aprobaciones concurrentes). Se maneja `DbUpdateConcurrencyException` explícitamente en Application.

## Decisiones de persistencia

- **Base de datos**: SQL Server (LocalDB aceptable en desarrollo). No se aprueban NoSQL ni almacenes adicionales (Redis, etc.) sin ADR.
- **ORM**: Entity Framework Core, con `Configurations` Fluent API por entidad (no Data Annotations en las entidades de Domain — las anotaciones de EF como `rowVersion` se configuran solo en Infrastructure).
- **Sin DELETE físico**: los registros de solicitud y auditoría no se eliminan; el estado (`Cancelled`, `Rejected`, etc.) representa el fin del ciclo de vida.

## Elección tecnológica: reglas de aprobación

- Framework web: ASP.NET Core MVC con Razor Views. **Prohibidos frameworks SPA** (React, Angular, Vue) sin un Architecture Decision Record (ADR) aprobado explícitamente.
- Cualquier librería de terceros distinta a FluentValidation (ya aprobada) requiere justificación documentada antes de agregarse — no se instalan dependencias "por si acaso".
- Abstracción de tiempo: `TimeProvider` nativo de .NET (no una interfaz custom) para reducir código propio de infraestructura.

## Anti-patrones prohibidos

- **Lógica de negocio en el controlador**: cualquier `if` que decida una regla de negocio (saldo, fechas, permisos) fuera de Domain/Application.
- **Entidad de Domain con atributos de infraestructura**: anotaciones EF, atributos de serialización web, etc. dentro de `Domain/Entities`.
- **Validación de negocio delegada a FluentValidation**: FluentValidation es solo para validación de entrada (formato, requerido, longitud) — nunca para reglas de negocio como "saldo insuficiente" o "solicitud solapada".
- **Confiar en cálculos del cliente**: el número de días solicitados, el saldo estimado, etc. SIEMPRE se recalculan en el servidor, nunca se aceptan tal cual desde el formulario.
- **Servicios de background como solución para lógica que puede probarse pura**: por ejemplo, el cálculo de días hábiles es un método puro en Domain, no un job.
- **Deuda técnica heredada del scaffold**: cuando el scaffold inicial (proyecto MVC vacío) no respeta la separación en capas, la estrategia es reescritura directa (crear los 4 proyectos desde cero) en lugar de migración incremental que arrastre la violación de capas.

## Objetivos de rendimiento que condicionan decisiones de diseño

| Operación | p95 objetivo |
|---|---|
| Consulta de saldo individual | ≤ 300 ms |
| Creación de solicitud | ≤ 1 s |
| Aprobación/rechazo | ≤ 1 s |
| Listado paginado | ≤ 2 s |
| Página MVC estándar | ≤ 500 ms |

Estos objetivos justifican decisiones como: paginación server-side obligatoria en listados, evitar N+1 queries en la bandeja de aprobador (que muestra saldo de cada empleado), y no bloquear el hilo de request con operaciones de background (la auto-expiración corre como `BackgroundService` separado, no dentro del ciclo de request).
