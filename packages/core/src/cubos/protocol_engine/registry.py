"""Command registry: @protocol_command decorator and CommandRegistry singleton."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, Type, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model

logger = logging.getLogger(__name__)

_FALLBACK_MAX_LEN = 80


def _fallback_summary(args: Dict[str, Any]) -> str:
    if not args:
        return ""
    rendered = ", ".join(f"{key}={value}" for key, value in args.items())
    if len(rendered) <= _FALLBACK_MAX_LEN:
        return rendered
    return rendered[: _FALLBACK_MAX_LEN - 1] + "\u2026"


class RegisteredCommand:
    """A registered command: name, handler, Pydantic schema, display summary."""

    __slots__ = ("name", "handler", "schema", "summary")

    def __init__(
        self,
        name: str,
        handler: Callable,
        schema: Type[BaseModel],
        summary: Callable[[Dict[str, Any]], str] | None = None,
    ) -> None:
        self.name = name
        self.handler = handler
        self.schema = schema
        self.summary = summary

    def describe(self, args: Dict[str, Any]) -> str:
        """Return a one-line, operator-readable summary of *args*.

        Uses the command's registered ``summary`` formatter when it has one.
        Formatters live next to the command precisely so display logic does
        not accumulate in UI code; a command without one (or whose formatter
        raises on unusual args) falls back to a generic key=value rendering
        rather than failing the caller.
        """
        if self.summary is not None:
            try:
                return self.summary(args)
            except Exception:  # noqa: BLE001 - display must never break a run
                logger.debug(
                    "summary formatter for %r failed; using fallback",
                    self.name, exc_info=True,
                )
        return _fallback_summary(args)


class CommandRegistry:
    """Singleton registry mapping YAML command names to handlers + schemas."""

    _instance: CommandRegistry | None = None

    def __init__(self) -> None:
        self._commands: Dict[str, RegisteredCommand] = {}

    @classmethod
    def instance(cls) -> CommandRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton for test isolation."""
        cls._instance = None

    def register(
        self,
        name: str,
        handler: Callable,
        schema: Type[BaseModel],
        summary: Callable[[Dict[str, Any]], str] | None = None,
    ) -> None:
        if name in self._commands:
            raise ValueError(f"Protocol command '{name}' is already registered.")
        self._commands[name] = RegisteredCommand(
            name=name, handler=handler, schema=schema, summary=summary,
        )

    def get(self, name: str) -> RegisteredCommand:
        if name not in self._commands:
            available = ", ".join(sorted(self._commands.keys()))
            raise KeyError(
                f"Unknown protocol command '{name}'. "
                f"Available commands: {available}"
            )
        return self._commands[name]

    @property
    def command_names(self) -> list[str]:
        return sorted(self._commands.keys())


def _build_schema_from_signature(
    name: str, func: Callable,
) -> Type[BaseModel]:
    """Introspect a function's signature and build a strict Pydantic model.

    Skips ``self`` and ``context`` parameters (context is injected at runtime).
    All remaining parameters become required schema fields unless they have
    default values in the function signature.
    """
    sig = inspect.signature(func)
    globalns = dict(getattr(func, "__globals__", {}))
    if "ProtocolContext" not in globalns:
        from .runtime import ProtocolContext

        globalns["ProtocolContext"] = ProtocolContext
    type_hints = get_type_hints(func, globalns=globalns, localns=globalns)
    field_definitions: Dict[str, Any] = {}

    skip_params = {"self", "context"}

    for param_name, param in sig.parameters.items():
        if param_name in skip_params:
            continue

        annotation = type_hints.get(param_name)
        if annotation is None:
            annotation = (
                param.annotation
                if param.annotation != inspect.Parameter.empty
                else str
            )

        if param.default != inspect.Parameter.empty:
            field_definitions[param_name] = (annotation, param.default)
        else:
            field_definitions[param_name] = (annotation, ...)

    schema_class_name = f"{name.title().replace('_', '')}Schema"
    model = create_model(
        schema_class_name,
        __config__=ConfigDict(extra="forbid"),
        **field_definitions,
    )
    return model


def protocol_command(
    name: str | None = None,
    summary: Callable[[Dict[str, Any]], str] | None = None,
) -> Callable:
    """Decorator that registers a function as a protocol YAML command.

    Usage::

        @protocol_command("move")
        def move(context: ProtocolContext, instrument: str, position: str) -> None:
            ...

    Or with the function name as the command name::

        @protocol_command()
        def move(context: ProtocolContext, instrument: str, position: str) -> None:
            ...

    ``summary`` is an optional ``args -> str`` formatter used to render this
    command as one readable line in operator-facing views (the run step list).
    It receives the step's compiled args and must not raise; a formatter that
    does is caught and replaced by the generic fallback. Keeping it beside the
    command means a newly added command ships its own display instead of
    needing a UI change::

        @protocol_command("transfer", summary=_transfer_summary)
    """
    def decorator(func: Callable) -> Callable:
        cmd_name = name or func.__name__
        schema = _build_schema_from_signature(cmd_name, func)
        CommandRegistry.instance().register(cmd_name, func, schema, summary)
        func._protocol_command_name = cmd_name  # type: ignore[attr-defined]
        func._protocol_schema = schema  # type: ignore[attr-defined]
        return func
    return decorator
