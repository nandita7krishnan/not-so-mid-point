"""Per-request dependencies, handed to nodes through LangGraph's config."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .providers.llm import LLMClient
from .providers.maps import MapsClient


@dataclass
class RunDeps:
    maps: MapsClient
    llm: Optional[LLMClient] = None


def deps_from_config(config: dict[str, Any]) -> RunDeps:
    deps = (config or {}).get("configurable", {}).get("deps")
    if deps is None:
        raise RuntimeError("RunDeps missing from graph config; call run_graph().")
    return deps
