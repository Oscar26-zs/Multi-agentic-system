"""Agente de Seguridad (security_agent).

Qué hace:
    Revisa la solución propuesta/implementada desde la perspectiva de
    seguridad: autenticación, autorización, validación de entradas y
    vulnerabilidades del OWASP Top 10.

Responsabilidad dentro del sistema:
    Control de calidad en seguridad: consulta el RAG de seguridad y las tools
    MCP de security scanning, y registra hallazgos que deben resolverse antes
    de que el Reviewer apruebe.

Se espera que contenga cuando se implemente:
    - Prompt de sistema con el rol de security engineer.
    - Consulta al retriever RAG de seguridad (security-guidelines.md,
      owasp-guidelines.md).
    - Invocación de tools MCP de security scanning (análisis estático, escaneo
      de dependencias, detección de secretos hardcodeados).
    - Structured output con hallazgos (severidad, descripción, recomendación).
"""
