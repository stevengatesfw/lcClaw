# -*- coding: utf-8 -*-
"""Install lightweight stubs for optional heavy deps before test import.

Most lcClaw unit tests do not need the full agentscope/agentscope_runtime
stack. When those packages are not installed, a meta-path finder serves
generic stub modules so tool-level tests remain runnable without the heavy
dependency tree. Stubs are only installed when the real packages are absent.
"""
import importlib.abc
import importlib.machinery
import sys
import types

_STUB_PREFIXES = (
    "agentscope",
    "agentscope_runtime",
    "anthropic",
    "openai",
    "google",
    "ollama",
    "fastapi",
    "websocket",
    "aiofiles",
    "json_repair",
    "shortuuid",
)


def _missing_packages() -> tuple[str, ...]:
    missing = []
    for prefix in _STUB_PREFIXES:
        try:
            module = __import__(prefix)
        except ModuleNotFoundError:
            missing.append(prefix)
            continue
        # A bare/partial ``google`` install still breaks ``google.genai``.
        if prefix == "google":
            try:
                from google import genai  # noqa: F401
            except Exception:
                missing.append(prefix)
                # Drop the broken partial module so the stub finder can own it.
                sys.modules.pop("google", None)
                sys.modules.pop("google.genai", None)
    return tuple(missing)


class _TextBlock(dict):
    def __init__(self, type: str = "text", text: str = "", **extra):  # noqa: A002
        super().__init__(type=type, text=text, **extra)


class _ToolResponse:
    def __init__(self, content=None, metadata=None, **extra):
        self.content = content or []
        self.metadata = metadata
        for key, value in extra.items():
            setattr(self, key, value)


_SPECIALS = {
    "TextBlock": _TextBlock,
    "ToolResponse": _ToolResponse,
}


def _generic_symbol(name: str):
    def _init(self, *args, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    return type(name, (), {"__init__": _init})


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if name in _SPECIALS:
            return _SPECIALS[name]
        symbol = _generic_symbol(name)
        setattr(self, name, symbol)
        return symbol


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, prefixes: tuple[str, ...]):
        self._prefixes = prefixes

    def _matches(self, fullname: str) -> bool:
        return any(
            fullname == prefix or fullname.startswith(prefix + ".")
            for prefix in self._prefixes
        )

    def find_spec(self, fullname, path=None, target=None):
        if fullname in sys.modules or not self._matches(fullname):
            return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(self, spec):
        return _StubModule(spec.name)

    def exec_module(self, module):
        module.__path__ = []


def _install_stub_tree(fullname: str) -> _StubModule:
    parts = fullname.split(".")
    parent = None
    current = ""
    module = None
    for part in parts:
        current = part if not current else f"{current}.{part}"
        module = sys.modules.get(current)
        if module is None:
            module = _StubModule(current)
            module.__path__ = []
            sys.modules[current] = module
        if parent is not None:
            setattr(parent, part, module)
        parent = module
    return module


_MISSING = _missing_packages()
if _MISSING:
    sys.meta_path.insert(0, _StubFinder(_MISSING))
    if "google" in _MISSING:
        # ``from google import genai`` / ``from google.genai import errors``
        # require a real submodule tree, not AttributeError placeholders.
        _install_stub_tree("google.genai.errors")
        _install_stub_tree("google.genai.types")
