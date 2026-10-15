"""session-triage — detect an abandoned session, diagnose its cause, heal, resume once.

The engine is deliberately pure-Python and takes injected collaborators (a read-only sqlite
connection, a ledger path, an ``inject`` callable, a clock) so every decision is testable against a
fixture profile with no live gateway.
"""

from .config import Config
from .engine import PassResult, run_pass

__all__ = ["Config", "PassResult", "run_pass"]
