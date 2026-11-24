"""Memory for the operations director.

Two different things live here, deliberately kept apart:

* ``store.py`` — the four work-memory files the director owns (profile,
  experiments, decisions, lessons). Read by the workshop, written only by the
  director.
* ``facts.py`` — tenant facts that can be searched, injected into later tasks,
  and revoked by a human. Append-only, status folded on read.

The package used to be a single ``memory.py``; it became a package in Step 6 so
that ``facts.py`` could sit next to it. ``from ..memory import MemoryStore`` is
unchanged, which is the only import anyone used.
"""

from .store import MemoryStore

__all__ = ["MemoryStore"]
