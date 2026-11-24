"""Governed question answering (ARCHITECTURE.md sections 4 and 5).

    from answer import ask
    a = ask("What did tail N412AM's AOG in September cost us?", role="exec", provider="rules")
    print(a.render())
"""
from answer.core import REFUSAL, Answer, ask

__all__ = ["ask", "Answer", "REFUSAL"]
