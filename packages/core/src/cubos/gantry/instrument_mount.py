from __future__ import annotations

import logging
import math
from typing import Any, TYPE_CHECKING

from cubos.instruments.base_instrument import BaseInstrument
from cubos.instruments.lighting.interface import LightingInstrument

from .errors import LocationNotFound

if TYPE_CHECKING:
    from cubos.gantry import Gantry

# A position is either an (x, y, z) tuple or any object with x, y, z attributes
# (e.g. a labware object sitting at a fixed deck location).
Position = Any

# GRBL reports WPos to 3 decimals; anything under this is not lateral travel.
_XY_SAME_TOL_MM = 0.01


class InstrumentedGantry:
    """Gantry plus the instruments mounted on it.

    Holds one physical Gantry controller and a dictionary of named instruments.
    Each instrument's offset_x, offset_y, and depth describe its mounting
    position relative to the gantry head so runtime motion can calculate the
    gantry pose needed to put an instrument tip at a deck-frame coordinate.
    """

    def __init__(
        self,
        controller: Gantry,
        instruments: dict[str, BaseInstrument] | None = None,
        expected_grbl_settings: dict[str, float] | None = None,
        safe_z: float | None = None,
    ):
        self.controller = controller
        self.instruments: dict[str, BaseInstrument] = instruments or {}
        self.expected_grbl_settings = (
            dict(expected_grbl_settings) if expected_grbl_settings else None
        )
        self.safe_z = safe_z
        self.last_commanded_pose: dict[str, Any] | None = None
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def move(
        self,
        instrument: str | BaseInstrument,
        position: Position,
        travel_z: float | None = None,
    ) -> None:
        """Move the gantry so that *instrument* arrives at *position*.

        Accounts for the instrument's offset_x, offset_y, and depth so the
        gantry head ends up at the right place for the instrument tip to be
        at the requested (x, y, z). Validates that the target is finite
        (no NaN/Inf) before commanding the gantry.

        ``travel_z``, if given, is an instrument-tip Z held during XY
        travel: the gantry lifts/lowers to it before moving XY, then
        descends/ascends to the target Z.

        When ``travel_z`` is omitted and the move changes XY, the gantry
        travels at ``multi_tool_safe_travel_z`` so every tool mounted on
        the head clears the deck, not just *instrument*. A move that only
        changes Z (engage, descend to an action plane, retract) is sent
        as-is. Pass an explicit ``travel_z`` to travel XY lower than the
        ceiling (e.g. ``scan`` interwell hops inside one plate).
        """
        instr = self._resolve_instrument(instrument)
        x, y, z = self._resolve_position(position)
        self._validate_finite_xyz(x, y, z, instr.name)
        if travel_z is not None and not math.isfinite(travel_z):
            raise ValueError(
                f"non-finite travel_z={travel_z} for instrument {instr.name!r}."
            )
        depth = self._effective_depth(instr)
        gantry_x = x - instr.offset_x
        gantry_y = y - instr.offset_y
        gantry_z = z + depth
        if travel_z is None and self._xy_changes(gantry_x, gantry_y):
            travel_z = self.multi_tool_safe_travel_z(instr)
        gantry_travel_z = travel_z + depth if travel_z is not None else None
        self.logger.info(
            "Moving %s to (%.3f, %.3f, %.3f) -> gantry (%.3f, %.3f, %.3f)",
            instr.name, x, y, z, gantry_x, gantry_y, gantry_z,
        )
        self.last_commanded_pose = {
            "instrument": instr.name,
            "instrument_position": (x, y, z),
            "gantry_position": (gantry_x, gantry_y, gantry_z),
            "travel_z": gantry_travel_z,
        }
        self.controller.move_to(
            gantry_x, gantry_y, gantry_z, travel_z=gantry_travel_z,
        )

    def move_to_labware(
        self,
        instrument: str | BaseInstrument,
        labware: Position,
    ) -> None:
        """Travel *instrument* to ``safe_z`` above a labware target.

        Emits a single ``move`` with ``travel_z = self.safe_z``: the
        gantry lifts/lowers to that absolute deck-frame Z plane at
        the current XY, travels XY at ``safe_z``, and ends above the
        target without descending. Higher-level commands
        (``measure``, ``scan``, ...) follow up with a raw ``move`` to
        descend to the per-labware action plane.
        """
        if self.safe_z is None:
            raise ValueError(
                "InstrumentedGantry.safe_z is not set. Configure `cnc.safe_z` "
                "in the gantry YAML or build the InstrumentedGantry with "
                "`safe_z=...`."
            )
        instr = self._resolve_instrument(instrument)
        x, y, z = self._resolve_position(labware)
        self._validate_finite_xyz(x, y, z, instr.name)
        self.move(instr, (x, y, self.safe_z), travel_z=self.multi_tool_safe_travel_z(instr))

    def multi_tool_safe_travel_z(
        self, instrument: str | BaseInstrument,
    ) -> float | None:
        """Instrument-tip Z at which XY travel clears every mounted tool.

        The carriage rides ``working_volume.z_max`` (or ``safe_z`` if that
        is higher for this instrument), so a tool hanging lower than
        *instrument* still clears the deck. ``safe_z`` remains the hover
        and retract plane. Returns ``None`` only when neither ``safe_z``
        nor a working volume is configured.
        """
        instr = self._resolve_instrument(instrument)
        candidates = []
        if self.safe_z is not None:
            candidates.append(float(self.safe_z))
        # The multi-tool guarantee needs working_volume.z_max on the controller
        # config; without it only safe_z (the active tool's own clearance) applies.
        ceiling = self._gantry_z_ceiling()
        if ceiling is not None:
            candidates.append(ceiling - self._effective_depth(instr))
        if not candidates:
            self.logger.warning(
                "No safe_z or working_volume configured; XY travel for %s "
                "will not lift first.", instr.name,
            )
            return None
        return max(candidates)

    def _xy_changes(self, gantry_x: float, gantry_y: float) -> bool:
        """Whether a move to gantry XY leaves the current XY.

        Unknown position (no parsable GRBL status, or a controller double
        without coordinates) counts as a change so the move lifts first.
        Connection errors propagate.
        """
        try:
            coords = self.controller.get_coordinates()
            current_x = float(coords["x"])
            current_y = float(coords["y"])
        except LocationNotFound as exc:
            self.logger.warning(
                "Position read failed before move (%s); lifting before XY.", exc,
            )
            return True
        except (KeyError, TypeError, ValueError):
            return True
        if not (math.isfinite(current_x) and math.isfinite(current_y)):
            return True
        return (
            abs(gantry_x - current_x) > _XY_SAME_TOL_MM
            or abs(gantry_y - current_y) > _XY_SAME_TOL_MM
        )

    def _gantry_z_ceiling(self) -> float | None:
        """Gantry-frame z_max from the controller config, if available."""
        config = getattr(self.controller, 'config', None)
        if not isinstance(config, dict):
            return None
        volume = config.get('working_volume')
        if not isinstance(volume, dict):
            return None
        z_max = volume.get('z_max')
        if isinstance(z_max, (int, float)) and math.isfinite(float(z_max)):
            return float(z_max)
        return None

    def _validate_finite_xyz(self, x: float, y: float, z: float, instr_name: str) -> None:
        for label, value in (("x", x), ("y", y), ("z", z)):
            if not math.isfinite(value):
                raise ValueError(
                    f"non-finite {label}={value} for instrument {instr_name!r}."
                )

    @staticmethod
    def _effective_depth(instr: BaseInstrument) -> float:
        """Read ``instr.effective_depth`` and guard the value as finite."""
        depth = instr.effective_depth
        if not isinstance(depth, (int, float)) or not math.isfinite(depth):
            raise ValueError(
                f"non-finite effective_depth={depth!r} for instrument {instr.name!r}."
            )
        return float(depth)

    def object_position(
        self, obj: str | BaseInstrument | Any,
    ) -> tuple[float, float]:
        """Return the current (x, y) position of an instrument or object."""
        if isinstance(obj, str):
            obj = self._resolve_instrument(obj)

        if isinstance(obj, BaseInstrument):
            coords = self.controller.get_coordinates()
            return (
                coords["x"] + obj.offset_x,
                coords["y"] + obj.offset_y,
            )

        return (obj.x, obj.y)

    def connect_instruments(self) -> None:
        """Connect all mounted instruments."""
        for name, instrument in self.instruments.items():
            self.logger.info("Connecting instrument: %s", name)
            instrument.connect()

    def disconnect_instruments(self) -> None:
        """Disconnect all instruments, logging errors without re-raising.

        Lighting is commanded off first, best-effort, so an aborted run
        never leaves lights on.
        """
        for name, instrument in self.instruments.items():
            if isinstance(instrument, LightingInstrument):
                try:
                    instrument.all_off()
                except Exception:
                    self.logger.exception(
                        "Failed to turn off lighting instrument '%s'", name,
                    )
        for name, instrument in self.instruments.items():
            try:
                self.logger.info("Disconnecting instrument: %s", name)
                instrument.disconnect()
            except Exception:
                self.logger.exception(
                    "Failed to disconnect instrument '%s'", name,
                )

    def _resolve_instrument(
        self, instrument: str | BaseInstrument,
    ) -> BaseInstrument:
        """Look up an instrument by name or return it directly."""
        if isinstance(instrument, str):
            if instrument not in self.instruments:
                raise KeyError(
                    f"Unknown instrument '{instrument}'. "
                    f"Available: {', '.join(sorted(self.instruments.keys()))}"
                )
            return self.instruments[instrument]
        return instrument

    @staticmethod
    def _resolve_position(position: Position) -> tuple[float, float, float]:
        """Convert a position tuple or labware object to (x, y, z)."""
        if isinstance(position, tuple):
            return position
        return (position.x, position.y, position.z)
