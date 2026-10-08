"""Protocol: executable sequence of validated protocol steps."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .runtime import ProtocolContext, ProtocolStep


@dataclass(frozen=True)
class ProtocolSetup:
    """Gantry/deck pairing a Python-authored protocol carries for hardware runs.

    YAML-loaded protocols have no setup metadata unless explicitly wrapped
    later; only protocols built via ``ProtocolBuilder.with_setup(...)`` know
    which machine and deck they belong to.
    """

    gantry_path: str | Path
    deck_path: str | Path


class Protocol:
    """An executable protocol: a validated, ordered list of steps.

    Two execution entry points sit at different layers:

    ``execute(context)`` is the low-level primitive — it runs the steps
    against an already-prepared :class:`ProtocolContext` and owns no
    hardware lifecycle::

        protocol = load_protocol_from_yaml("my_protocol.yaml")
        context = ProtocolContext(gantry=gantry, deck=deck)
        protocol.execute(context)

    ``run()`` is the high-level, user-facing entry point. It requires setup
    metadata and owns the full hardware session (connect, prepare, execute,
    cleanup), delegating step execution to ``execute(context)``::

        protocol = (
            ProtocolBuilder.with_setup(gantry_path=..., deck_path=...)
            .add_home()
            .add_move(instrument="asmi", position="plate.A1")
            .build()
        )
        protocol.validate()   # offline check, no hardware
        protocol.run()        # full hardware session
    """

    def __init__(
        self,
        steps: List[ProtocolStep],
        source_path: Path | None = None,
        positions: Dict[str, Any] | None = None,
        setup: ProtocolSetup | None = None,
    ) -> None:
        self._steps = list(steps)
        self.source_path = source_path
        self.positions = positions or {}
        self.setup = setup
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def steps(self) -> List[ProtocolStep]:
        return list(self._steps)

    def __len__(self) -> int:
        return len(self._steps)

    def execute(self, context: ProtocolContext) -> List[Any]:
        """Execute all steps sequentially against a prepared context.

        Low-level primitive: the caller owns the gantry connection and
        instrument lifecycle. Returns the list of step results.
        """
        if getattr(context.deck, "planning_enabled", False) is True:
            from .routing import prepare_planning_context

            if context.routing_session is None:
                prepare_planning_context(self, context)

        self.logger.info(
            "Running protocol (%d steps)%s",
            len(self._steps),
            f" from {self.source_path}" if self.source_path else "",
        )
        results: List[Any] = []
        for step in self._steps:
            result = step.execute(context)
            results.append(result)
        self.logger.info("Protocol complete.")
        return results

    def validate(self) -> None:
        """Validate the protocol offline against its setup metadata.

        Runs the same gantry/deck loading, bounds, and semantic validation as
        a hardware run, but with an offline gantry — it never connects to
        hardware and never executes any steps.

        Raises:
            ValueError: If the protocol has no setup metadata.
        """
        if self.setup is None:
            raise ValueError(
                "Protocol has no setup metadata to validate against. Build it "
                "with ProtocolBuilder.with_setup(gantry_path=..., deck_path=...) "
                "before calling validate()."
            )
        from .setup import setup_protocol

        setup_protocol(
            self.setup.gantry_path,
            self.setup.deck_path,
            self,
            mock_mode=True,
        )

    def run(
        self,
        campaign: str | None = None,
        data_store: Any | None = None,
        protocol_config: str | None = None,
        fluid_state_id: int | None = None,
        initial_fluids: str | Path | Dict[str, Any] | None = None,
    ) -> List[Any]:
        """Run the protocol on hardware as a full session.

        High-level, user-facing entry point: requires setup metadata and runs
        the full hardware lifecycle (load gantry, connect, prepare, connect
        instruments, health check, execute, disconnect).

        Measurement data is persisted by default. A campaign is created for the
        run, recording this protocol's gantry/deck paths from its setup
        metadata. ``campaign`` optionally supplies the campaign description.

        Args:
            campaign: Optional campaign description.
            data_store: Optional :class:`data.DataStore` to write into. When
                omitted, a default store is created and closed automatically.
            protocol_config: Optional identifier recorded on the campaign for
                the source that built this protocol (e.g. a module path).
            fluid_state_id: Existing deck-bound fluid state to resume. The
                stored deck fingerprint must match this protocol's deck.
            initial_fluids: Seed YAML path or mapping used only when creating a
                new fluid state for this run.

        Raises:
            ValueError: If the protocol has no setup metadata.
        """
        if self.setup is None:
            raise ValueError(
                "Protocol.run() needs setup metadata to drive hardware. Build "
                "it with ProtocolBuilder.with_setup(gantry_path=..., "
                "deck_path=...), or call execute(context) with a prepared "
                "context."
            )

        from .setup import run_on_hardware

        return run_on_hardware(
            self.setup.gantry_path,
            self.setup.deck_path,
            self,
            data_store=data_store,
            campaign_description=campaign,
            protocol_config=protocol_config,
            fluid_state_id=fluid_state_id,
            initial_fluids=initial_fluids,
        )

    def __repr__(self) -> str:
        cmds = ", ".join(s.command_name for s in self._steps)
        return f"Protocol([{cmds}])"
