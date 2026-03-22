"""Thread-safe LLM seed chaining for retail GEPA evals (simulator + qualitative judge).

``evaluation_seed`` in YAML is the master. When ``llm_seed_chain`` is enabled, register
:class:`LLMSeedChainCallback` with GEPA and call :meth:`LLMSeedChain.read` for each eval.
"""

from __future__ import annotations

import random
import threading
from typing import Any

_MAX_LLM_SEED = 2**31 - 1


def retail_llm_seed_master_from_gepa_cfg(gepa_cfg: dict[str, Any]) -> int:
    """Resolve master int for :class:`LLMSeedChain` and static simulator seeds.

    Prefers ``evaluation_seed``; falls back to legacy ``seed``; then ``42``.
    """
    for key in ("evaluation_seed", "seed"):
        v = gepa_cfg.get(key)
        if v is not None and str(v).strip() != "":
            return int(v)
    return 42


class LLMSeedChain:
    """Per-iteration LLM ``seed`` for retail simulation evals inside GEPA.

    - **Before** the first ``on_iteration_start`` with ``iteration >= 1``: :meth:`read`
      returns the **master** (from ``evaluation_seed``), used for baseline / iteration-0 work.
    - **Each** ``on_iteration_start`` with ``iteration >= 1``:
      ``generated = Random(current).randint(1, 10**9)``,
      ``current = (master + generated) % (2**31 - 1)`` (non-zero).
    """

    def __init__(self, master: int) -> None:
        m = int(master) % _MAX_LLM_SEED
        self._master = m if m != 0 else 1
        self._current = self._master
        self._lock = threading.Lock()

    def read(self) -> int:
        with self._lock:
            return self._current

    def advance_on_iteration_start(self, iteration: int) -> None:
        """Advance after baseline; GEPA passes ``iteration`` = 1 for the first opt loop."""
        if iteration < 1:
            return
        with self._lock:
            prev = self._current
            gen = random.Random(prev).randint(1, 10**9)
            nxt = (self._master + gen) % _MAX_LLM_SEED
            self._current = nxt if nxt != 0 else 1


class LLMSeedChainCallback:
    """GEPA callback: advance :class:`LLMSeedChain` on ``on_iteration_start``."""

    def __init__(self, chain: LLMSeedChain) -> None:
        self._chain = chain

    def on_iteration_start(self, event: dict[str, Any]) -> None:
        try:
            it = int(event.get("iteration", 0))
        except (TypeError, ValueError):
            return
        self._chain.advance_on_iteration_start(it)
