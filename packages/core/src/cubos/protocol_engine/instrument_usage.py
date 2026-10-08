"""Which mounted instruments a protocol's steps actually reference.

Used to connect only the instruments a protocol needs (see
``protocol_engine.setup.run_on_hardware`` and
``gantry.session.GantrySession.run_protocol``) instead of every instrument
mounted on the gantry -- e.g. a protocol that only drives the ASMI probe
should not power up and calibrate an idle pipette.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cubos.instruments.lighting.interface import LightingInstrument

if TYPE_CHECKING:
    from cubos.gantry.instrument_mount import InstrumentedGantry

    from .protocol import Protocol

# Every pipette-family command resolves its instrument to the fixed
# "pipette" gantry key (`commands.pipette._get_pipette`) rather than taking
# an `instrument` arg, so there is nothing to read off `step.args` for them.
PIPETTE_COMMANDS = frozenset({
    "aspirate", "blowout", "mix", "pick_up_tip", "transfer", "drop_tip",
    "serial_transfer", "rinse_well", "flush_pipette", "purge_pipette",
    "clear_well",
})

# Commands that never touch a mounted instrument.
NO_INSTRUMENT_COMMANDS = frozenset({"home", "pause", "breakpoint"})


def required_instrument_names(
    protocol: "Protocol", gantry: "InstrumentedGantry",
) -> set[str]:
    """Names of instruments *protocol*'s steps reference on *gantry*.

    Reads ``step.args["instrument"]`` for the common case (``move``,
    ``measure``, ``scan``, ``capture``, ``decap``, ``cap``, ``cure``,
    ``rinse``, ``set_lights``). Special-cases the pipette command family
    (fixed ``"pipette"`` key, no ``instrument`` arg) and ``image_well``
    (``camera`` field, plus its ``lights`` field -- which, like
    ``commands.camera._resolve_lighting``, resolves to *gantry*'s sole
    mounted ``LightingInstrument`` when omitted).
    """
    names: set[str] = set()
    for step in getattr(protocol, "steps", ()):
        command = step.command_name
        if command in NO_INSTRUMENT_COMMANDS:
            continue
        if command in PIPETTE_COMMANDS:
            names.add("pipette")
            continue
        if command == "image_well":
            names.add(step.args["camera"])
            lights = step.args.get("lights")
            if lights is None:
                names |= _sole_lighting_instrument_name(gantry)
            elif lights != "none":
                names.add(lights)
            continue
        instrument = step.args.get("instrument")
        if instrument is not None:
            names.add(instrument)
    return names


def _sole_lighting_instrument_name(gantry: "InstrumentedGantry") -> set[str]:
    """Gantry's one ``LightingInstrument`` name, or empty if zero/ambiguous.

    Mirrors ``commands.camera._resolve_lighting``'s own auto-discovery:
    zero matches is a no-op there, and more than one raises at execution
    time -- nothing here needs to be connected for a step that will abort
    anyway.
    """
    lighting = [
        name
        for name, instrument in gantry.instruments.items()
        if isinstance(instrument, LightingInstrument)
    ]
    return {lighting[0]} if len(lighting) == 1 else set()
