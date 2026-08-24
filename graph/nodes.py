"""Nodos del grafo de LangGraph.

Qué hace:
    Envuelve a cada agente en una función nodo con la firma estándar de
    LangGraph (estado -> actualizaciones parciales del estado).

Responsabilidad dentro del sistema:
    Desacopla a los agentes del motor de orquestación: cada nodo delega en su
    agente correspondiente y persiste la salida en el estado compartido.

Se espera que contenga cuando se implemente:
    - Un nodo por agente: product_node, architect_node, developer_node,
      security_node, testing_node y reviewer_node.
    - Manejo de excepciones por nodo y propagación de errores al estado.
    - Hooks de tracing de Langfuse alrededor de cada nodo.
"""
