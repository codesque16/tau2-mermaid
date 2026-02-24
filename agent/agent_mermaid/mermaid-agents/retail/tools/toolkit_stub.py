"""Minimal toolkit and tool wrapper when tau2 is not installed. Used by tools.py."""

from __future__ import annotations

import inspect
from enum import Enum
from typing import Any, Callable, Dict, Optional, TypeVar

from pydantic import BaseModel, create_model

try:
    from tau2.environment.db import DB
except ImportError:
    DB = None  # type: ignore[misc, assignment]

T = TypeVar("T")

TOOL_ATTR = "__tool__"
TOOL_TYPE_ATTR = "__tool_type__"


class ToolType(str, Enum):
    READ = "read"
    WRITE = "write"
    THINK = "think"
    GENERIC = "generic"


def is_tool(tool_type: ToolType = ToolType.READ):
    def decorator(func: Callable) -> Callable:
        setattr(func, TOOL_ATTR, True)
        setattr(func, TOOL_TYPE_ATTR, tool_type)
        return func
    return decorator


def _params_from_signature(func: Callable) -> type[BaseModel]:
    sig = inspect.signature(func)
    params = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        anno = param.annotation if param.annotation is not inspect.Parameter.empty else Any
        default = param.default if param.default is not inspect.Parameter.empty else ...
        params[name] = (anno, default)
    return create_model(f"{func.__name__}_params", **params)  # type: ignore[call-overload]


class _ToolWrapper:
    """Minimal tool-like object with openai_schema and callable."""

    def __init__(self, func: Callable, name: str | None = None):
        self._func = func
        self.name = name or func.__name__
        self.short_desc = (func.__doc__ or "").strip().split("\n")[0].strip()
        self.params = _params_from_signature(func)

    @property
    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.short_desc or self.name,
                "parameters": self.params.model_json_schema(),
            },
        }

    def __call__(self, **kwargs: Any) -> Any:
        return self._func(**kwargs)


def as_tool(func: Callable, **kwargs: Any) -> _ToolWrapper:
    return _ToolWrapper(func, **kwargs)


class ToolKitType(type):
    def __init__(cls, name: str, bases: tuple, attrs: dict) -> None:
        func_tools: Dict[str, Callable] = {}
        for attr_name, method in attrs.items():
            if isinstance(method, property):
                method = method.fget
            if callable(method) and getattr(method, TOOL_ATTR, False):
                func_tools[attr_name] = method

        @property
        def _func_tools(self) -> Dict[str, Callable]:
            all_tools = dict(func_tools)
            for base in bases:
                if hasattr(base, "_func_tools"):
                    try:
                        all_tools.update(getattr(base, "_func_tools").fget(self))
                    except Exception:
                        pass
            return all_tools

        cls._func_tools = _func_tools
        super().__init__(name, bases, attrs)


class ToolKitBase(metaclass=ToolKitType):
    def __init__(self, db: Optional[T] = None) -> None:
        self.db: Optional[T] = db

    @property
    def tools(self) -> Dict[str, Callable]:
        return {name: getattr(self, name) for name in self._func_tools.keys()}

    def use_tool(self, tool_name: str, **kwargs: Any) -> str:
        if tool_name not in self.tools:
            raise ValueError(f"Tool '{tool_name}' not found.")
        result = self.tools[tool_name](**kwargs)
        if isinstance(result, str):
            return result
        if hasattr(result, "model_dump"):
            import json
            return json.dumps(result.model_dump())
        return str(result)

    def get_tools(self) -> Dict[str, _ToolWrapper]:
        return {name: as_tool(tool) for name, tool in self.tools.items()}

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self.tools
