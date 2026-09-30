"""Documented constants for the PANDA-BEAR -> CubOS import.

Tool offsets are copied verbatim from PANDA-BEAR's own tool table
(``src/panda_lib/hardware/grbl_cnc_mill/tools.json`` in the PANDA-BEAR repo).
PANDA-BEAR's driver computes ``gantry = tool_target + tool_offset`` per axis
(``grbl_cnc_mill/driver.py:_calculate_target_coordinates``, PANDA-BEAR repo);
:mod:`.conversion` mirrors that exact formula to turn a PANDA-BEAR DB
tool-point coordinate into a CubOS home-origin gantry-frame coordinate.

Override the defaults at the CLI with ``--tools-json`` if the physical tool
mounts change; see :mod:`.tools_json`.
"""

from __future__ import annotations

from typing import Mapping, NamedTuple

from cubos.deck.labware.container_role import PROCESS as _CONTAINER_ROLE_PROCESS
from cubos.deck.labware.container_role import STOCK as _CONTAINER_ROLE_STOCK
from cubos.deck.labware.container_role import WASTE as _CONTAINER_ROLE_WASTE
from cubos.deck.labware.tip_rack import DEFAULT_TIP_LENGTH_MM as _DEFAULT_TIP_LENGTH_MM


class ToolOffset(NamedTuple):
    """A PANDA-BEAR tool offset: ``gantry = tool_target + offset`` per axis."""

    x: float
    y: float
    z: float


# PANDA-BEAR's tools.json, transcribed. Only the tools actually referenced by
# the imported labware are needed (pipette, electrode); the rest are kept for
# completeness/documentation and possible future --tools-json overrides.
DEFAULT_TOOL_OFFSETS: Mapping[str, ToolOffset] = {
    "pipette": ToolOffset(x=-115.9, y=-6.1, z=100.0),
    "electrode": ToolOffset(x=56.7, y=-3.8, z=100.0),
    "decapper": ToolOffset(x=-62.9, y=-5.1, z=61.5),
    "lens": ToolOffset(x=0.0, y=0.0, z=0.0),
    "center": ToolOffset(x=0.0, y=0.0, z=0.0),
}

PIPETTE_FRAME_TOOL = "pipette"
ELECTRODE_FRAME_TOOL = "electrode"

# cubos.deck.labware.tip_rack.DEFAULT_TIP_LENGTH_MM (59.3mm): the Opentrons
# standard 300uL tip length. panda_tips.tip_length is NULL for every row in
# the production snapshot; see packages/core/configs/deck/panda_import_resolutions.yaml.
DEFAULT_TIP_LENGTH_MM = _DEFAULT_TIP_LENGTH_MM

# Physical dimensions of the standard 20mL vial used on every PANDA stock/
# waste/electrode-bath position. panda_vials.height/radius are generic
# working-volume-measurement defaults (66.0 / 14.0), not the vial's true
# physical envelope, so they are not read from the DB.
VIAL_HEIGHT_MM = 57.0
VIAL_DIAMETER_MM = 28.0

# panda_vials.category values.
VIAL_CATEGORY_STOCK = 0
VIAL_CATEGORY_WASTE = 1
VIAL_CATEGORY_ELECTRODE = 2

# Feature 05: category -> generic CubOS container role (see
# cubos.deck.labware.container_role). The electrode/bath category maps to
# the generic "process" role -- it is neither a solution reservoir (stock)
# nor a disposal sink (waste), just a container liquid is processed in.
VIAL_CATEGORY_ROLES: Mapping[int, str] = {
    VIAL_CATEGORY_STOCK: _CONTAINER_ROLE_STOCK,
    VIAL_CATEGORY_WASTE: _CONTAINER_ROLE_WASTE,
    VIAL_CATEGORY_ELECTRODE: _CONTAINER_ROLE_PROCESS,
}

ROUND_NDIGITS = 3  # 0.001mm determinism, matches cubos.deck.loader rounding.

# Feature 06: the raw source gantry YAML still declares the capper/decapper
# mount as generic `mounted_tool`/`mount_only` (CubOS had no dedicated
# capper instrument type when that mount entry was written). The importer
# upgrades it in place to the real `capper`/`pawduino` type+vendor (see
# cubos.instruments.capper) so the generated gantry config drives real
# decap/cap motion instead of being a calibration-only placeholder.
# `engage_depth_mm`/`capture_retries`/`capture_settle_s` are
# PLACEHOLDER values -- they parameterize the decap/cap motion sequence
# (cubos.protocol_engine.commands.capper) but were never measured against
# real PANDA hardware; confirm/recalibrate before trusting physical motion.
CAPPER_INSTRUMENT_KEY = "vial_capper_decapper"
CAPPER_ENGAGE_DEPTH_MM = -15.0
CAPPER_CAPTURE_RETRIES = 2
CAPPER_CAPTURE_SETTLE_S = 1.0

# Rows/wells/tips whose converted gantry position falls outside the gantry's
# working volume by more than this are flagged as warning-level conflicts
# (never blocking -- see packages/core/src/cubos/tools/import_panda_bear.py).
ENVELOPE_TOLERANCE_MM = 0.05

# Well-plate grid pitch residual above which a warning conflict is reported.
PITCH_RESIDUAL_TOLERANCE_MM = 0.2
