import importlib

import pytest

from cubos.protocol_engine.registry import CommandRegistry

_REAL_COMMANDS: set[str] | None = None


def restore_protocol_commands() -> None:
    """Rebuild the singleton registry from every module in the commands package."""
    import cubos.protocol_engine.commands as commands

    CommandRegistry.reset()
    for name in commands.__all__:
        importlib.reload(importlib.import_module(f"{commands.__name__}.{name}"))


@pytest.fixture(autouse=True)
def _real_protocol_commands():
    """Keep the real command set registered across tests that reset the singleton."""
    global _REAL_COMMANDS
    if _REAL_COMMANDS is None:
        import cubos.protocol_engine.commands  # noqa: F401

        _REAL_COMMANDS = set(CommandRegistry.instance().command_names)
    elif not _REAL_COMMANDS <= set(CommandRegistry.instance().command_names):
        restore_protocol_commands()
    yield
    if set(CommandRegistry.instance().command_names) != _REAL_COMMANDS:
        restore_protocol_commands()
