"""One-line display summaries for protocol commands.

Each function renders a compiled step's args as a short, operator-readable
line for the run step list -- ``transfer  stock.A1 -> plate.B3  500 uL``
rather than ``source=stock.A1, destination=plate.B3, volume_ul=500.0,
speed=50.0, ...``.

These live beside the commands (rather than in the Operator UI) so that a
newly added command ships its own display instead of requiring a matching UI
change. They are wired in via ``@protocol_command(..., summary=...)``.

Formatters are display-only and must never *fail a caller*. Required args are
indexed directly (``args["source"]``) rather than defaulted: a compiled step
always carries them, and on the malformed input where one is missing a
``KeyError`` is the desired outcome -- ``RegisteredCommand.describe`` catches
it and renders the generic ``key=value`` fallback, which is more useful to an
operator than a line of ``None``s. Only genuinely optional args use ``.get``.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

ARROW = "→"
MICROLITRE = "µL"


def _volume(value: Any) -> str:
    """Render a microlitre quantity without trailing-zero noise."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{value} {MICROLITRE}"
    if number == int(number):
        return f"{int(number)} {MICROLITRE}"
    return f"{number:g} {MICROLITRE}"


def _volumes(values: Sequence[Any] | None) -> str:
    if not values:
        return ""
    rendered = [_volume(value).removesuffix(f" {MICROLITRE}") for value in values]
    if len(rendered) > 4:
        rendered = rendered[:3] + ["…"]
    return f"[{', '.join(rendered)}] {MICROLITRE}"


def _position(value: Any) -> str:
    """Render a deck target, which may be a name or explicit coordinates."""
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(f"{part:g}" for part in value) + ")"
    if isinstance(value, dict):
        return "(" + ", ".join(f"{k}={v:g}" for k, v in value.items()) + ")"
    return str(value)


def _join(*parts: str) -> str:
    return "   ".join(part for part in parts if part)


def move(args: Dict[str, Any]) -> str:
    return _join(
        f"{args['instrument']} {ARROW} {_position(args['position'])}",
    )


def home(args: Dict[str, Any]) -> str:
    return "all axes"


def measure(args: Dict[str, Any]) -> str:
    return _join(
        f"{args['instrument']} @ {_position(args['position'])}",
        str(args.get("method", "measure")),
    )


def cure(args: Dict[str, Any]) -> str:
    intensity = args.get("intensity")
    detail = f"{args['exposure_time']}s"
    if intensity is not None:
        detail += f" @ {intensity}%"
    return _join(
        f"{args['instrument']} @ {_position(args['position'])}",
        detail,
    )


def scan(args: Dict[str, Any]) -> str:
    return _join(
        f"{args['instrument']} over {args['plate']}",
        str(args.get("method", "measure")),
    )


def decap(args: Dict[str, Any]) -> str:
    return str(args["vial"])


cap = decap


def pause(args: Dict[str, Any]) -> str:
    seconds = args["seconds"]
    reason = args.get("reason") or ""
    return _join(f"{seconds}s", reason)


def breakpoint_cmd(args: Dict[str, Any]) -> str:
    return str(args.get("message", ""))


def aspirate(args: Dict[str, Any]) -> str:
    return _join(
        _volume(args["volume_ul"]),
        f"from {args['position']}",
    )


def blowout(args: Dict[str, Any]) -> str:
    return f"at {args['position']}"


def mix(args: Dict[str, Any]) -> str:
    return _join(
        str(args["position"]),
        _volume(args["volume_ul"]),
        f"{args.get('cycles', 3)}x",
    )


def pick_up_tip(args: Dict[str, Any]) -> str:
    return f"from {args['position']}"


def drop_tip(args: Dict[str, Any]) -> str:
    return f"at {args['position']}"


def transfer(args: Dict[str, Any]) -> str:
    liquid_class = args.get("liquid_class")
    return _join(
        f"{args['source']} {ARROW} {args['destination']}",
        _volume(args["volume_ul"]),
        f"({liquid_class})" if liquid_class else "",
    )


def serial_transfer(args: Dict[str, Any]) -> str:
    volumes = args.get("volumes")
    volume_range = args.get("volume_range")
    if volumes:
        amount = _volumes(volumes)
    elif volume_range:
        low, high = volume_range[0], volume_range[-1]
        amount = f"{low:g}–{high:g} {MICROLITRE}"
    else:
        amount = ""
    return _join(
        f"{args['source']} {ARROW} {args['plate']} "
        f"{str(args.get('axis', '')).upper()}",
        amount,
    )


def rinse_well(args: Dict[str, Any]) -> str:
    return _join(
        str(args["well"]),
        _volume(args["volume_ul"]),
        f"{args.get('cycles', 3)} cycles",
    )


def rinse(args: Dict[str, Any]) -> str:
    return _join(
        f"{args['instrument']} @ {args['vial']}",
        f"3 dips ({args['measurement_height']:g} mm)",
    )


def flush_pipette(args: Dict[str, Any]) -> str:
    return _join(
        _volume(args["volume_ul"]),
        f"{args.get('cycles', 1)} cycles",
    )


def purge_pipette(args: Dict[str, Any]) -> str:
    return _join(_volume(args["volume_ul"]), "to waste")


def set_lights(args: Dict[str, Any]) -> str:
    if args.get("all_off"):
        return _join(str(args["instrument"]), "off")
    return _join(
        str(args["instrument"]),
        f"{args.get('channel')} {args.get('brightness')}%",
    )


def capture(args: Dict[str, Any]) -> str:
    label = args.get("label")
    return _join(str(args["instrument"]), str(label) if label else "")


def image_well(args: Dict[str, Any]) -> str:
    return _join(
        f"{args['camera']} @ {_position(args['well'])}",
        str(args.get("mode", "standard")),
    )


def clear_well(args: Dict[str, Any]) -> str:
    target = args.get("target_volume_ul", 0.0)
    explicit = args.get("volume_ul")
    amount = _volume(explicit) if explicit is not None else f"down to {_volume(target)}"
    return _join(str(args["well"]), amount)


__all__ = [
    "aspirate",
    "blowout",
    "breakpoint_cmd",
    "cap",
    "capture",
    "clear_well",
    "decap",
    "drop_tip",
    "flush_pipette",
    "home",
    "image_well",
    "measure",
    "mix",
    "move",
    "pause",
    "pick_up_tip",
    "purge_pipette",
    "rinse_well",
    "scan",
    "serial_transfer",
    "set_lights",
    "transfer",
]
