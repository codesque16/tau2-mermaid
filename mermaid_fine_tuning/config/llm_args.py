"""Centralized loader for the (llm, llm_args) pair used by the data-gen pipeline.

Matches the tau2 convention used by VertexUserSimulator: `llm` is the model
string ("vertex_ai/gemini-3.1-pro-preview") and `llm_args` is a dict with
optional fields (temperature, max_tokens, reasoning_level, include_thoughts,
seed, pricing, ...).

Edit `load_llm_config()` to match how your project supplies this pair.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field


@dataclass
class LLMConfig:
    llm: str
    llm_args: dict = field(default_factory=dict)


def load_llm_config() -> LLMConfig:
    """Return the (llm, llm_args) pair for the data-gen pipeline.

    Two reasonable starting points are below. Uncomment one to match your
    project, or write your own loader.
    """

    # ---- Option A: import from your tau2 config module ----
    # from tau2.config import data_gen_llm_config
    # return LLMConfig(llm=data_gen_llm_config.llm, llm_args=dict(data_gen_llm_config.llm_args or {}))

    # ---- Option B: env-driven defaults ----
    return LLMConfig(
        llm=os.environ.get("DATA_GEN_LLM", "vertex_ai/gemini-3.1-pro-preview"),
        llm_args={
            "temperature": 0.7,
            # Data-gen never wants thought parts in the returned text.
            # Setting reasoning_level enables thinking; leave unset to disable.
            # "reasoning_level": "MEDIUM",
            # "include_thoughts": False,
            # "max_tokens": 16384,
            # "seed": 42,
        },
    )


if __name__ == "__main__":
    cfg = load_llm_config()
    print(f"llm={cfg.llm}")
    print(f"llm_args={cfg.llm_args}")
