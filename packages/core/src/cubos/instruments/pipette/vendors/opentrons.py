import math
from typing import Optional

from cubos.instruments.controllers.pawduino import (
    PawduinoLink,
    PawduinoLinkCommandError,
    PawduinoLinkError,
    PawduinoLinkTimeoutError,
)
from cubos.instruments.pipette.interface import PipetteInstrument
from cubos.instruments.pipette.exceptions import (
    PipetteCommandError,
    PipetteConfigError,
    PipetteConnectionError,
    PipetteTimeoutError,
)
from cubos.instruments.pipette.liquid_class import build_liquid_classes
from cubos.instruments.pipette.models import (
    AspirateResult,
    PipetteStatus,
    PlungerPipetteConfig,
    PIPETTE_MODELS,
)

_CMD_HOME = 10
_CMD_MOVE_TO = 11
_CMD_ASPIRATE = 12
_CMD_DISPENSE = 13
_CMD_STATUS = 14
_CMD_DRIP_STOP = 28

# The firmware replies only after a motion completes, and plunger motion is
# slow: full 55 mm travel at the default velocity takes ~35 s, and a failed
# homing attempt takes up to 60 s. Match PANDA-BEAR's 120 s command deadline.
_MOTION_TIMEOUT = 120.0

# Firmware interprets the optional speed argument as stepper steps/second
# (its stepDelay floor makes small values ~16x slower than intended, slow
# enough to blow the serial timeout on a full-travel move). Passing 0 tells
# the firmware to use its own calibrated default velocity.
# TODO(iter): map CubOS speed semantics onto steps/second explicitly.
_FIRMWARE_DEFAULT_SPEED = 0.0


class OpentronsPipette(PipetteInstrument):
    """Driver for Opentrons pipettes via Arduino serial (Pawduino firmware).

    Pass ``offline=True`` for dry runs — simulates plunger state in memory.
    """

    CONFIG_FIELD_CHOICES = {
        "pipette_model": sorted(PIPETTE_MODELS.keys()),
    }

    def __init__(
        self,
        pipette_model: str = "p300_single_gen2",
        port: str = "",
        baud_rate: int = 115200,
        command_timeout: float = 30.0,
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,
        liquid_classes: Optional[dict] = None,
        **kwargs,
    ):
        super().__init__(
            name=name, offset_x=offset_x, offset_y=offset_y,
            depth=depth,
            offline=offline,
        )
        if pipette_model not in PIPETTE_MODELS:
            raise PipetteConfigError(
                f"Unknown pipette model '{pipette_model}'. "
                f"Available: {', '.join(sorted(PIPETTE_MODELS.keys()))}"
            )
        self._config: PlungerPipetteConfig = PIPETTE_MODELS[pipette_model]
        # Per-liquid-class stroke-volume correction (multiplier + offset_ul),
        # keyed by an operator-chosen name; empty/disabled unless configured.
        # See cubos.instruments.pipette.liquid_class for the parametric form.
        self._liquid_classes = build_liquid_classes(liquid_classes)
        self._port = port
        self._baud_rate = baud_rate
        self._command_timeout = command_timeout
        self._link: Optional[PawduinoLink] = None
        self._has_tip = False
        self._attached_tip_extension = 0.0
        self._position_mm = 0.0
        self._is_homed = False
        self._is_primed = False

    @property
    def config(self) -> PlungerPipetteConfig:
        return self._config

    @property
    def attached_tip_extension(self) -> float:
        return self._attached_tip_extension

    @property
    def effective_depth(self) -> float:
        return self.depth + self._attached_tip_extension

    def set_attached_tip_extension(self, extension_mm: float) -> None:
        if (
            isinstance(extension_mm, bool)
            or not isinstance(extension_mm, (int, float))
            or not math.isfinite(float(extension_mm))
            or float(extension_mm) < 0.0
        ):
            raise PipetteConfigError(
                f"attached tip extension must be a non-negative finite number, "
                f"got {extension_mm!r}."
            )
        self._attached_tip_extension = float(extension_mm)

    def clear_attached_tip_extension(self) -> None:
        self._attached_tip_extension = 0.0

    # ── BaseInstrument interface ──────────────────────────────────────────

    def connect(self) -> None:
        if self._offline:
            self.logger.info("Pipette connected (offline)")
            return
        # The capper and lights share this Arduino via one link per port.
        try:
            self._link = PawduinoLink.acquire(self._port, self._baud_rate)
            self._link.connect(timeout=self._command_timeout)
        except PawduinoLinkError as exc:
            self._link = None
            raise PipetteConnectionError(str(exc)) from exc

        try:
            status = self.get_status()
        except (PipetteCommandError, PipetteTimeoutError) as exc:
            self._release_link()
            raise PipetteConnectionError(
                f"Arduino did not respond after connect: {exc}"
            ) from exc

        # The reset also wipes the firmware's plunger reference: it believes
        # position 0.0 wherever the plunger physically sits, and no motion
        # command guards against that. Re-establish a real reference now.
        if not status.is_homed:
            self.logger.info("Plunger not homed after connect; homing and priming")
            try:
                self.home()
                self.prime()
            except (PipetteCommandError, PipetteTimeoutError) as exc:
                self._release_link()
                raise PipetteConnectionError(
                    f"Plunger home/prime after connect failed: {exc}"
                ) from exc

        self.logger.info(
            "Connected to %s on %s", self._config.name, self._port
        )

    def disconnect(self) -> None:
        if self._offline:
            self.logger.info("Pipette disconnected (offline)")
            return
        self._release_link()
        self.logger.info("Disconnected from pipette")

    def health_check(self) -> bool:
        if self._offline:
            return True
        if self._link is None or not self._link.is_open:
            return False
        try:
            self.get_status()
            return True
        except (PipetteCommandError, PipetteTimeoutError):
            return False

    def warm_up(self) -> None:
        self.home()
        self.prime()

    # ── Pipette-specific commands ─────────────────────────────────────────

    def home(self) -> None:
        if self._offline:
            self._position_mm = self._config.zero_position
            self._is_homed = True
            return
        try:
            self._send_command(_CMD_HOME, timeout=_MOTION_TIMEOUT)
        except PipetteCommandError:
            # Firmware gives up after ~31 mm of upward travel per attempt,
            # but full plunger travel is 55 mm: a plunger parked low needs a
            # second leg to reach the limit switch.
            self.logger.info("Homing fell short of the limit switch; retrying")
            self._send_command(_CMD_HOME, timeout=_MOTION_TIMEOUT)
        self._position_mm = self._config.zero_position
        self._is_homed = True

    def prime(self, speed: float = 50.0) -> None:
        if self._offline:
            self._position_mm = self._config.prime_position
            self._is_primed = True
            return
        self._send_command(
            _CMD_MOVE_TO, self._config.prime_position, _FIRMWARE_DEFAULT_SPEED,
            timeout=_MOTION_TIMEOUT,
        )
        self._position_mm = self._config.prime_position
        self._is_primed = True

    def aspirate(self, volume_ul: float, speed: float = 50.0) -> AspirateResult:
        self._validate_volume(volume_ul)
        mm_travel = volume_ul * self._config.mm_to_ul
        if self._offline:
            self._position_mm += mm_travel
            return AspirateResult(
                success=True, volume_ul=volume_ul, position_mm=self._position_mm
            )
        response = self._send_command(
            _CMD_ASPIRATE, mm_travel, _FIRMWARE_DEFAULT_SPEED,
            timeout=_MOTION_TIMEOUT
        )
        position = self._parse_position(response)
        return AspirateResult(
            success=True, volume_ul=volume_ul, position_mm=position
        )

    def dispense(self, volume_ul: float, speed: float = 50.0) -> AspirateResult:
        self._validate_volume(volume_ul)
        mm_travel = volume_ul * self._config.mm_to_ul
        if self._offline:
            self._position_mm -= mm_travel
            return AspirateResult(
                success=True, volume_ul=volume_ul, position_mm=self._position_mm
            )
        response = self._send_command(
            _CMD_DISPENSE, mm_travel, _FIRMWARE_DEFAULT_SPEED,
            timeout=_MOTION_TIMEOUT
        )
        position = self._parse_position(response)
        return AspirateResult(
            success=True, volume_ul=volume_ul, position_mm=position
        )

    def blowout(self, speed: float = 50.0) -> None:
        if self._offline:
            self._position_mm = self._config.blowout_position
            return
        self._send_command(
            _CMD_MOVE_TO, self._config.blowout_position, _FIRMWARE_DEFAULT_SPEED,
            timeout=_MOTION_TIMEOUT,
        )

    def pick_up_tip(self, speed: float = 50.0) -> None:
        if not self._offline:
            self._send_command(
                _CMD_MOVE_TO, self._config.zero_position, _FIRMWARE_DEFAULT_SPEED,
                timeout=_MOTION_TIMEOUT,
            )
        self._has_tip = True

    def drop_tip(self, speed: float = 50.0) -> None:
        if self._offline:
            self._has_tip = False
            self.clear_attached_tip_extension()
            self._position_mm = self._config.prime_position
            self._is_primed = True
            return
        self._send_command(
            _CMD_MOVE_TO,
            self._config.drop_tip_position,
            _FIRMWARE_DEFAULT_SPEED,
            timeout=_MOTION_TIMEOUT,
        )
        self._has_tip = False
        self.clear_attached_tip_extension()
        self._position_mm = self._config.drop_tip_position
        # The tip is already off; a failure returning to prime must not look
        # like a failed drop. Leave the plunger parked low — connect() will
        # re-home it next time.
        try:
            self._send_command(
                _CMD_MOVE_TO,
                self._config.prime_position,
                _FIRMWARE_DEFAULT_SPEED,
                timeout=_MOTION_TIMEOUT,
            )
        except (PipetteCommandError, PipetteTimeoutError) as exc:
            self.logger.warning(
                "Return to prime after tip drop failed (%s); plunger parked "
                "low, will re-home on next connect", exc,
            )
            return
        self._position_mm = self._config.prime_position
        self._is_primed = True

    def get_status(self) -> PipetteStatus:
        if self._offline:
            return PipetteStatus(
                is_homed=self._is_homed,
                position_mm=self._position_mm,
                max_volume=self._config.max_volume,
                has_tip=self._has_tip,
                is_primed=self._is_primed,
            )
        # A status body always carries max_vol; requiring it keeps stray OK
        # lines (e.g. a late boot banner) from being taken as the response.
        response = self._send_command(_CMD_STATUS, expect="max_vol")
        parsed = self._parse_key_value(response)
        return PipetteStatus(
            is_homed=parsed.get("homed", 0) == 1,
            position_mm=float(parsed.get("pos", 0.0)),
            max_volume=float(parsed.get("max_vol", self._config.max_volume)),
            has_tip=self._has_tip,
            is_primed=parsed.get("primed", 0) == 1,
        )

    def drip_stop(self, volume_ul: float = 5.0, speed: float = 50.0) -> None:
        self._validate_positive_bounded_volume(volume_ul)
        if self._offline:
            return
        mm_travel = volume_ul * self._config.mm_to_ul
        self._send_command(
            _CMD_DRIP_STOP, mm_travel, _FIRMWARE_DEFAULT_SPEED,
            timeout=_MOTION_TIMEOUT,
        )

    # ── Private helpers ───────────────────────────────────────────────────

    def _send_command(
        self,
        code: int,
        *args: float,
        timeout: Optional[float] = None,
        expect: Optional[str] = None,
    ) -> str:
        if self._link is None:
            raise PipetteCommandError("Not connected to Arduino")
        wait = self._command_timeout if timeout is None else timeout
        try:
            return self._link.send_command(
                code, *args, timeout=wait, expect=expect,
            )
        except PawduinoLinkTimeoutError as exc:
            raise PipetteTimeoutError(str(exc)) from exc
        except PawduinoLinkCommandError as exc:
            raise PipetteCommandError(str(exc)) from exc

    @staticmethod
    def _parse_key_value(response: str) -> dict[str, float]:
        """Parse ``OK:{...}`` bodies with quoted or bare keys.

        The Pawduino firmware emits JSON-quoted keys
        (``OK:{"homed":1,"pos":0.00,"max_vol":300.00}``); bare keys are
        tolerated for older firmware and tests.
        """
        result: dict[str, float] = {}
        body = response.removeprefix("OK:").strip()
        if body.startswith("{") and body.endswith("}"):
            body = body[1:-1]
        for pair in body.split(","):
            if ":" not in pair:
                continue
            key, _, val = pair.partition(":")
            try:
                result[key.strip().strip('"')] = float(val.strip().strip('"'))
            except ValueError:
                continue
        return result

    @staticmethod
    def _parse_position(response: str) -> float:
        parsed = OpentronsPipette._parse_key_value(response)
        return float(parsed.get("pos", 0.0))

    def _release_link(self) -> None:
        if self._link is not None:
            self._link.disconnect()
            self._link = None

    def _validate_volume(self, volume_ul: float) -> None:
        if (
            isinstance(volume_ul, bool)
            or not isinstance(volume_ul, (int, float))
            or not math.isfinite(float(volume_ul))
            or not (self._config.min_volume <= float(volume_ul) <= self._config.max_volume)
        ):
            raise PipetteCommandError(
                f"Volume {volume_ul!r} uL is outside {self._config.name} range "
                f"{self._config.min_volume}-{self._config.max_volume} uL"
            )

    def _validate_positive_bounded_volume(self, volume_ul: float) -> None:
        if (
            isinstance(volume_ul, bool)
            or not isinstance(volume_ul, (int, float))
            or not math.isfinite(float(volume_ul))
            or not (0.0 < float(volume_ul) <= self._config.max_volume)
        ):
            raise PipetteCommandError(
                f"Volume {volume_ul!r} uL is outside {self._config.name} range "
                f"0-{self._config.max_volume} uL"
            )
