from dataclasses import dataclass
from enum import Enum


class PipetteFamily(Enum):
    OT2 = "OT2"
    FLEX = "FLEX"
    PICUS2 = "PICUS2"


@dataclass(frozen=True, kw_only=True)
class PipetteConfig:
    """What a pipette model can do, independent of how it is actuated.

    Deliberately free of plunger geometry: a vendor whose pipette owns its
    own piston (Sartorius Picus 2) has volumes and channels but no
    millimetres. Drivers that *do* push a plunger declare that separately in
    :class:`PlungerPipetteConfig`.

    ``volume_increment_ul`` is the smallest settable volume step; ``0.0``
    means continuous (a stepper can stop anywhere).
    """

    name: str
    family: PipetteFamily
    channels: int
    max_volume: float
    min_volume: float
    volume_increment_ul: float = 0.0


@dataclass(frozen=True, kw_only=True)
class PlungerPipetteConfig(PipetteConfig):
    """Adds the plunger geometry a CubOS-driven stepper needs.

    These are properties of *our* actuator (an Arduino pushing the plunger of
    a bare pipette body), not of the pipette model, which is why they live
    here rather than on :class:`PipetteConfig`.
    """

    zero_position: float
    prime_position: float
    blowout_position: float
    drop_tip_position: float
    mm_to_ul: float


@dataclass(frozen=True)
class PipetteStatus:
    """Snapshot of pipette state returned by get_status()."""

    is_homed: bool
    position_mm: float
    max_volume: float
    has_tip: bool
    is_primed: bool
    # Battery-powered vendors only; None where the instrument is mains- or
    # host-powered and has no charge state to report.
    battery_percent: float | None = None

    @property
    def is_valid(self) -> bool:
        return self.max_volume > 0 and self.position_mm >= 0


@dataclass(frozen=True)
class AspirateResult:
    """Result of an aspirate or dispense operation."""

    success: bool
    volume_ul: float
    # Plunger position. 0.0 on vendors that expose no position readback --
    # read `loaded_volume_ul` instead, which both kinds of driver can answer.
    position_mm: float
    loaded_volume_ul: float | None = None


@dataclass(frozen=True)
class MixResult:
    """Result of a mix (repeated two-height aspirate/dispense) operation."""

    success: bool
    volume_ul: float
    cycles: int


# ── Pipette model registry ───────────────────────────────────────────────────
# P300 uses real calibrated values from PANDA-BEAR.
# Other models have placeholder positions that need hardware calibration.

PIPETTE_MODELS: dict[str, PlungerPipetteConfig] = {
    # OT-2 single-channel
    "p20_single_gen2": PlungerPipetteConfig(
        name="p20_single_gen2",
        family=PipetteFamily.OT2,
        channels=1,
        max_volume=20.0,
        min_volume=1.0,
        zero_position=0.0,
        prime_position=5.0,       # placeholder
        blowout_position=7.0,     # placeholder
        drop_tip_position=10.0,   # placeholder
        mm_to_ul=0.025,           # placeholder
    ),
    "p300_single_gen2": PlungerPipetteConfig(
        name="p300_single_gen2",
        family=PipetteFamily.OT2,
        channels=1,
        max_volume=200.0,
        min_volume=20.0,
        zero_position=0.0,
        prime_position=36.0,      # calibrated from PANDA-BEAR
        blowout_position=46.0,    # calibrated from PANDA-BEAR
        drop_tip_position=60.0,   # calibrated from PANDA-BEAR
        mm_to_ul=0.1098,          # calibrated from PANDA-BEAR
    ),
    "p1000_single_gen2": PlungerPipetteConfig(
        name="p1000_single_gen2",
        family=PipetteFamily.OT2,
        channels=1,
        max_volume=1000.0,
        min_volume=100.0,
        zero_position=0.0,
        prime_position=40.0,      # placeholder
        blowout_position=50.0,    # placeholder
        drop_tip_position=65.0,   # placeholder
        mm_to_ul=0.55,            # placeholder
    ),
    # OT-2 multi-channel
    "p20_multi_gen2": PlungerPipetteConfig(
        name="p20_multi_gen2",
        family=PipetteFamily.OT2,
        channels=8,
        max_volume=20.0,
        min_volume=1.0,
        zero_position=0.0,
        prime_position=5.0,       # placeholder
        blowout_position=7.0,     # placeholder
        drop_tip_position=10.0,   # placeholder
        mm_to_ul=0.025,           # placeholder
    ),
    "p300_multi_gen2": PlungerPipetteConfig(
        name="p300_multi_gen2",
        family=PipetteFamily.OT2,
        channels=8,
        max_volume=200.0,
        min_volume=20.0,
        zero_position=0.0,
        prime_position=36.0,      # placeholder (same as single P300)
        blowout_position=46.0,    # placeholder
        drop_tip_position=60.0,   # placeholder
        mm_to_ul=0.1098,          # placeholder
    ),
    # Flex single-channel
    "flex_1channel_50": PlungerPipetteConfig(
        name="flex_1channel_50",
        family=PipetteFamily.FLEX,
        channels=1,
        max_volume=50.0,
        min_volume=1.0,
        zero_position=0.0,
        prime_position=8.0,       # placeholder
        blowout_position=11.0,    # placeholder
        drop_tip_position=15.0,   # placeholder
        mm_to_ul=0.04,            # placeholder
    ),
    "flex_1channel_1000": PlungerPipetteConfig(
        name="flex_1channel_1000",
        family=PipetteFamily.FLEX,
        channels=1,
        max_volume=1000.0,
        min_volume=5.0,
        zero_position=0.0,
        prime_position=40.0,      # placeholder
        blowout_position=50.0,    # placeholder
        drop_tip_position=65.0,   # placeholder
        mm_to_ul=0.55,            # placeholder
    ),
    # Flex multi-channel
    "flex_8channel_50": PlungerPipetteConfig(
        name="flex_8channel_50",
        family=PipetteFamily.FLEX,
        channels=8,
        max_volume=50.0,
        min_volume=1.0,
        zero_position=0.0,
        prime_position=8.0,       # placeholder
        blowout_position=11.0,    # placeholder
        drop_tip_position=15.0,   # placeholder
        mm_to_ul=0.04,            # placeholder
    ),
    "flex_8channel_1000": PlungerPipetteConfig(
        name="flex_8channel_1000",
        family=PipetteFamily.FLEX,
        channels=8,
        max_volume=1000.0,
        min_volume=5.0,
        zero_position=0.0,
        prime_position=40.0,      # placeholder
        blowout_position=50.0,    # placeholder
        drop_tip_position=65.0,   # placeholder
        mm_to_ul=0.55,            # placeholder
    ),
    "flex_96channel_1000": PlungerPipetteConfig(
        name="flex_96channel_1000",
        family=PipetteFamily.FLEX,
        channels=96,
        max_volume=1000.0,
        min_volume=5.0,
        zero_position=0.0,
        prime_position=40.0,      # placeholder
        blowout_position=50.0,    # placeholder
        drop_tip_position=65.0,   # placeholder
        mm_to_ul=0.55,            # placeholder
    ),
}


# ── Sartorius Picus 2 model registry ─────────────────────────────────────────
# Every figure is a published vendor specification (Picus 2 datasheet, rev.
# 07|2024), not a calibrated value: the pipette owns its piston, so there is
# no CubOS-side plunger geometry to measure.
#
# Only the models Ursa runs are registered; the other single-channel
# variants are a four-line addition each, with no driver change. Multichannel
# variants are deliberately absent -- an 8-channel pipette moves 8x the
# commanded volume while the deck model records a single well, so registering
# one would produce silently wrong fluid accounting.

PICUS2_MODELS: dict[str, PipetteConfig] = {
    # LH-747021
    "picus2_1ch_10": PipetteConfig(
        name="picus2_1ch_10",
        family=PipetteFamily.PICUS2,
        channels=1,
        max_volume=10.0,
        min_volume=0.5,
        volume_increment_ul=0.01,
    ),
    # LH-747041
    "picus2_1ch_120": PipetteConfig(
        name="picus2_1ch_120",
        family=PipetteFamily.PICUS2,
        channels=1,
        max_volume=120.0,
        min_volume=5.0,
        volume_increment_ul=0.1,
    ),
    # LH-747081
    "picus2_1ch_1000": PipetteConfig(
        name="picus2_1ch_1000",
        family=PipetteFamily.PICUS2,
        channels=1,
        max_volume=1000.0,
        min_volume=50.0,
        volume_increment_ul=1.0,
    ),
    "picus2_1ch_5000": PipetteConfig(
        name="picus2_1ch_5000",
        family=PipetteFamily.PICUS2,
        channels=1,
        max_volume=5000.0,
        min_volume=100.0,
        volume_increment_ul=5.0,
    ),
}
