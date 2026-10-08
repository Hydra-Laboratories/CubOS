# Color campaign target modes

## Implementation

- Added explicit `camera` (default) and `rgb` target modes to the campaign models and generated campaign specs.
- RGB targets validate an sRGB triplet in the 0–255 range and derive Lab through `cubos.optimization.color.rgb_to_lab` (D65).
- RGB candidate measurements preserve the camera ROI, acquisition, and quality gates, record `frame_center` provenance, and avoid fabricating a camera reference profile or physical well identity.
- Added RGB preview/provenance API data and persisted target mode/RGB values in saved presets.

## Verification

- `python -m pytest services/api/tests/test_color_campaign.py services/api/tests/test_campaign_templates.py -q` — 28 passed.
- `python -m pytest services/api/tests/test_campaign_routes.py -q` — 11 passed.
- `python -m pytest packages/core/tests/protocol_engine/test_camera_commands.py -q` — 37 passed.
- `compileall` and `git diff --check` passed; no hardware actions were performed.

## Deferred / coordination

- New RGB-specific automated tests remain deferred per the task scope; focused model, builder, preview, and provenance checks were run offline.
- No rack-reset endpoint or inventory mutation was changed. Any rack reset or tip/fluid reconciliation must use the existing state workflow and be coordinated with the current physical inventory before a real campaign.
