import json
from pathlib import Path

import pytest
import yaml

from cubos.gantry.gantry_config import GantryConfig, GantryType, WorkingVolume

from cubos_api.models.campaigns import (
    CampaignPresetDocument,
    ColorCampaignPresetDraft,
    ColorCampaignSetup,
)
from cubos_api.services.campaign_templates import compile_trial, extract_result_objective
from cubos_api.services.color_campaign import (
    _four_component_initial_points,
    build_color_campaign,
    target_protocol,
)


SOURCE_PROTOCOL = """protocol:
- pick_up_tip: {position: tips.A1}
- transfer: {source: stocks.A1, destination: plate.A1, volume_ul: 100, source_height: '-20'}
- drop_tip: {position: waste}
- pick_up_tip: {position: tips.A2}
- transfer: {source: stocks.A2, destination: plate.A1, volume_ul: 100, source_height: '-20'}
- drop_tip: {position: waste}
- pick_up_tip: {position: tips.A3}
- transfer: {source: stocks.A3, destination: plate.A1, volume_ul: 100, source_height: '-20'}
- mix: {position: plate.A1, volume_ul: 60, cycles: 3, height: -7}
- drop_tip: {position: waste}
"""

SOURCE_PROTOCOL_WATER = """protocol:
- pick_up_tip: {position: tips.A1}
- transfer: {source: stocks.A4, destination: plate.A1, volume_ul: 150, source_height: '-22'}
- drop_tip: {position: waste}
- pick_up_tip: {position: tips.A2}
- transfer: {source: stocks.A1, destination: plate.A1, volume_ul: 50, source_height: '-22'}
- drop_tip: {position: waste}
- pick_up_tip: {position: tips.A3}
- transfer: {source: stocks.A2, destination: plate.A1, volume_ul: 50, source_height: '-22'}
- drop_tip: {position: waste}
- pick_up_tip: {position: tips.A4}
- transfer: {source: stocks.A3, destination: plate.A1, volume_ul: 50, source_height: '-22'}
- mix: {position: plate.A1, volume_ul: 60, cycles: 3, height: -2}
- drop_tip: {position: waste}
"""


def setup(**overrides) -> ColorCampaignSetup:
    values = dict(
        gantry_file="g.yaml",
        deck_file="d.yaml",
        source_protocol_file="source.yaml",
        target_well="plate.A1",
        target_lab=(42.0, 12.0, 18.0),
        reference_processing_profile_id="profile-v1",
        red_source="stocks.A1",
        yellow_source="stocks.A2",
        blue_source="stocks.A3",
        candidate_wells=[f"plate.A{index}" for index in range(2, 8)],
        camera_instrument="camera",
        roi_fraction=0.5,
        expected_center=(0.5, 0.5),
        expected_center_source="operator_selected",
        mock_mode=True,
    )
    values.update(overrides)
    return ColorCampaignSetup(**values)


def candidate_wells(count: int) -> list[str]:
    wells = [
        f"plate.{chr(ord('A') + slot // 12)}{slot % 12 + 1}"
        for slot in range(96)
    ]
    return [well for well in wells if well != "plate.A1"][:count]


def gantry_config() -> GantryConfig:
    return GantryConfig(
        serial_port="mock",
        gantry_type=GantryType.CUB_XL,
        factory_z_travel_mm=100.0,
        working_volume=WorkingVolume(0.0, 300.0, 0.0, 180.0, 0.0, 94.601),
        safe_z=94.601,
        instruments={"camera": {"offset_x": 10.0, "offset_y": -4.0, "depth": 20.0}},
    )


def test_target_protocol_reads_selected_well_without_fluid_steps():
    document = yaml.safe_load(target_protocol("plate.C4", "camera", 0.45))
    assert document["protocol"] == [
        {"move": {"instrument": "camera", "position": "plate.C4"}},
        {"measure_color": {
            "instrument": "camera",
            "position": "plate.C4",
            "label": "color_target",
            "roi_fraction": 0.45,
        }},
    ]


def test_builder_writes_complete_protocol_and_campaign(tmp_path: Path):
    spec = build_color_campaign(setup(), tmp_path, source_protocol_yaml=SOURCE_PROTOCOL)
    protocol = yaml.safe_load((tmp_path / spec.protocol_file).read_text())["protocol"]
    assert protocol[1]["transfer"]["source"] == "stocks.A1"
    assert protocol[4]["transfer"]["source"] == "stocks.A2"
    assert protocol[7]["transfer"]["source"] == "stocks.A3"
    assert protocol[11]["measure_color"]["reference_lab"] == [42.0, 12.0, 18.0]
    assert protocol[11]["measure_color"]["reference_processing_profile_id"] == "profile-v1"
    assert protocol[11]["measure_color"]["expected_center"] == [0.5, 0.5]
    assert spec.objective.path == "11.delta_e_00"
    result = [None] * 11 + [{"delta_e_00": 2.4}]
    assert extract_result_objective(result, spec.objective.path) == 2.4
    assert spec.sum_constraint.total == 300
    assert spec.optimizer.initial_points[0] == {
        "red_ul": 200.0, "yellow_ul": 50.0, "blue_ul": 50.0,
    }
    assert spec.sequences[0].values == [f"plate.A{index}" for index in range(2, 8)]
    assert spec.sequences[1].values[:2] == ["tips.A1", "tips.A4"]


def test_half_volume_builder_scales_grid_and_initial_recipes_without_changing_mix(tmp_path: Path):
    spec = build_color_campaign(
        setup(
            total_volume_ul=150.0,
            component_min_ul=25.0,
            component_max_ul=100.0,
        ),
        tmp_path,
        source_protocol_yaml=SOURCE_PROTOCOL.replace("'-20'", "'-40'"),
    )
    protocol = yaml.safe_load((tmp_path / spec.protocol_file).read_text())["protocol"]

    assert spec.sum_constraint.total == 150.0
    assert {parameter.step for parameter in spec.parameters} == {2.5}
    assert spec.optimizer.initial_points[:3] == [
        {"red_ul": 100.0, "yellow_ul": 25.0, "blue_ul": 25.0},
        {"red_ul": 25.0, "yellow_ul": 100.0, "blue_ul": 25.0},
        {"red_ul": 25.0, "yellow_ul": 25.0, "blue_ul": 100.0},
    ]
    assert protocol[1]["transfer"]["source_height"] == "-40"
    assert protocol[8]["mix"] == {
        "position": "plate.A2", "volume_ul": 60, "cycles": 3, "height": -7,
    }
    assert sum(spec.optimizer.initial_points[0].values()) == 150.0


def test_half_volume_preset_round_trip():
    draft = ColorCampaignPresetDraft(
        source_protocol_file="source.yaml",
        candidate_wells=["plate.A4"],
        component_min_ul=25.0,
        component_max_ul=100.0,
        total_volume_ul=150.0,
    )
    restored = ColorCampaignPresetDraft.model_validate_json(draft.model_dump_json())
    assert restored.total_volume_ul == 150.0
    assert restored.component_min_ul == 25.0
    assert restored.component_max_ul == 100.0


def test_five_half_volume_campaigns_fit_stock_and_tip_envelopes(tmp_path: Path):
    spec = build_color_campaign(
        setup(
            batch_size=3,
            candidate_wells=candidate_wells(8),
            total_volume_ul=150.0,
            component_min_ul=25.0,
            component_max_ul=100.0,
        ),
        tmp_path,
        source_protocol_yaml=SOURCE_PROTOCOL,
    )
    allocated_tips = {
        tip
        for sequence in spec.sequences
        if sequence.name.endswith("_tip")
        for tip in sequence.values
    }
    seed_use_per_dye = sum(
        point["red_ul"] for point in spec.optimizer.initial_points[:3]
    )
    worst_case_per_dye_per_campaign = seed_use_per_dye + 5 * 100.0

    assert len(allocated_tips) == 17
    assert 5 * len(allocated_tips) == 85
    assert seed_use_per_dye == 150.0
    assert 5 * worst_case_per_dye_per_campaign == 3250.0
    assert 40 * spec.sum_constraint.total == 6000.0


def test_builder_appends_optional_photo_pose_without_shifting_objective(tmp_path: Path):
    position = (244.589, 144.0, 94.601)
    spec = build_color_campaign(
        setup(photo_position=position), tmp_path,
        source_protocol_yaml=SOURCE_PROTOCOL.replace("'-20'", "'-40'"),
        gantry_config=gantry_config(),
    )
    protocol = yaml.safe_load((tmp_path / spec.protocol_file).read_text())["protocol"]

    assert protocol[1]["transfer"]["source_height"] == "-40"
    assert protocol[8]["mix"]["height"] == -7
    assert protocol[-2] == {"move": {
        "instrument": "camera", "position": [254.589, 140.0, 74.601],
    }}
    mounted = gantry_config().instruments["camera"]
    resolved_head = (
        protocol[-2]["move"]["position"][0] - mounted["offset_x"],
        protocol[-2]["move"]["position"][1] - mounted["offset_y"],
        protocol[-2]["move"]["position"][2] + mounted["depth"],
    )
    assert resolved_head == position
    assert protocol[-1] == {"photo_pause": {
        "settle_seconds": 2.0, "capture_hold_seconds": 2.0, "well": "plate.A2",
    }}
    assert spec.objective.path == "11.delta_e_00"
    candidate = next(sequence for sequence in spec.sequences if sequence.name == "candidate_well")
    assert {binding.step_index for binding in candidate.bindings} == {1, 4, 7, 8, 10, 11, 13}


def test_builder_rejects_photo_pose_outside_carriage_bounds(tmp_path: Path):
    with pytest.raises(ValueError, match="outside the gantry working volume"):
        build_color_campaign(
            setup(photo_position=(301.0, 144.0, 94.601)),
            tmp_path,
            source_protocol_yaml=SOURCE_PROTOCOL,
            gantry_config=gantry_config(),
        )


def test_photo_position_round_trips_through_setup_and_preset():
    position = (244.589, 144.0, 94.601)
    restored = ColorCampaignSetup.model_validate_json(
        setup(photo_position=position).model_dump_json()
    )
    draft = ColorCampaignPresetDraft(
        source_protocol_file="source.yaml",
        candidate_wells=["plate.A4"],
        photo_position=restored.photo_position,
    )
    assert ColorCampaignPresetDraft.model_validate_json(
        draft.model_dump_json()
    ).photo_position == position


def test_builder_persists_validated_camera_target_provenance(tmp_path: Path):
    spec = build_color_campaign(
        setup(
            target_run_id="color-target-frozen",
            target_analysis_revision=3,
            target_well="plate.C7",
        ),
        tmp_path,
        source_protocol_yaml=SOURCE_PROTOCOL,
    )
    restored = type(spec).model_validate_json(spec.model_dump_json())
    assert restored.target_run_id == "color-target-frozen"
    assert restored.target_analysis_revision == 3
    assert restored.target_well == "plate.C7"
    assert restored.target_lab == (42.0, 12.0, 18.0)
    assert restored.reference_processing_profile_id == "profile-v1"

    preset = CampaignPresetDocument(
        name="physical target",
        spec=restored.model_copy(update={"fluid_state_id": None}),
        color_setup=ColorCampaignPresetDraft(
            source_protocol_file="source.yaml",
            target_mode="camera",
            target_well="plate.C7",
            candidate_wells=[f"plate.A{index}" for index in range(2, 8)],
        ),
    )
    preset_roundtrip = CampaignPresetDocument.model_validate_json(
        preset.model_dump_json()
    )
    assert preset_roundtrip.spec.target_run_id == "color-target-frozen"
    assert preset_roundtrip.spec.target_analysis_revision == 3


def test_campaign_spec_rejects_incomplete_camera_target_link(tmp_path: Path):
    spec = build_color_campaign(setup(), tmp_path, source_protocol_yaml=SOURCE_PROTOCOL)
    document = spec.model_dump()
    document["target_run_id"] = "target-without-revision"
    with pytest.raises(ValueError, match="both run and revision"):
        type(spec).model_validate(document)


def test_rgb_builder_ignores_browser_camera_placeholders_and_keeps_exact_rgb(tmp_path: Path):
    # The Operator keeps target_well and the RGB-preview Lab value in its setup
    # form even though an RGB campaign has no target capture provenance.
    spec = build_color_campaign(
        setup(
            target_mode="rgb",
            target_rgb=(223.0, 18.0, 226.0),
            target_lab=(52.0, 82.0, -52.0),
            target_well="plate.H12",
            expected_center=(0.5, 0.5),
            expected_center_source="frame_center",
            reference_processing_profile_id=None,
        ),
        tmp_path,
        source_protocol_yaml=SOURCE_PROTOCOL,
    )
    assert spec.target_mode == "rgb"
    assert spec.target_rgb == (223.0, 18.0, 226.0)
    assert spec.target_run_id is None
    assert spec.target_analysis_revision is None
    assert spec.target_well is None
    assert spec.target_lab is None
    assert spec.reference_processing_profile_id is None


def test_builder_allocates_each_trial_from_durable_available_tip_order(tmp_path: Path):
    available = [
        f"tips.{chr(ord('A') + index // 12)}{index % 12 + 1}"
        for index in range(3, 21)
    ]
    spec = build_color_campaign(
        setup(), tmp_path, available_tip_positions=available,
        source_protocol_yaml=SOURCE_PROTOCOL,
    )

    assert spec.sequences[1].values[:2] == ["tips.A4", "tips.A7"]
    assert spec.sequences[2].values[:2] == ["tips.A5", "tips.A8"]
    assert spec.sequences[3].values[:2] == ["tips.A6", "tips.A9"]


def test_builder_rejects_insufficient_durable_tip_capacity(tmp_path: Path):
    import pytest

    with pytest.raises(ValueError, match="needs 18 available tips"):
        build_color_campaign(
            setup(), tmp_path,
            available_tip_positions=[
                f"tips.{chr(ord('A') + index // 12)}{index % 12 + 1}"
                for index in range(3, 19)
            ],
            source_protocol_yaml=SOURCE_PROTOCOL,
        )


def test_target_cannot_be_reused_as_candidate():
    raw = setup().model_dump()
    raw["candidate_wells"] = ["plate.A1", "plate.A2", "plate.A3", "plate.A4", "plate.A5", "plate.A6"]
    import pytest
    with pytest.raises(ValueError, match="Target well"):
        ColorCampaignSetup.model_validate(raw)


def test_setup_accepts_target_lab_from_json_array():
    raw = setup().model_dump(mode="json")
    assert isinstance(raw["target_lab"], list)
    parsed = ColorCampaignSetup.model_validate_json(json.dumps(raw))
    assert parsed.target_lab == (42.0, 12.0, 18.0)


def test_four_component_initial_points_are_on_grid_in_bounds_and_sum_to_300():
    points = _four_component_initial_points(50.0, 200.0)
    assert len(points) == 6
    assert len({tuple(sorted(point.items())) for point in points}) == 6
    for point in points:
        assert set(point) == {"red_ul", "yellow_ul", "blue_ul", "water_ul"}
        assert abs(sum(point.values()) - 300.0) < 1e-9
        for name in ("red_ul", "yellow_ul", "blue_ul"):
            assert 50.0 <= point[name] <= 200.0
            assert abs((point[name] - 50.0) / 5.0 - round((point[name] - 50.0) / 5.0)) < 1e-9
        assert 5.0 <= point["water_ul"] <= 150.0
        assert abs((point["water_ul"] - 5.0) / 5.0 - round((point["water_ul"] - 5.0) / 5.0)) < 1e-9


def test_four_component_initial_points_respect_tight_custom_bounds():
    points = _four_component_initial_points(60.0, 90.0)
    assert points
    for point in points:
        assert abs(sum(point.values()) - 300.0) < 1e-9
        for name in ("red_ul", "yellow_ul", "blue_ul"):
            assert 60.0 <= point[name] <= 90.0
        assert point["water_ul"] >= 5.0
        assert point["water_ul"] <= 300.0 - 3 * 60.0


def test_source_steps_rejects_diluent_shape_without_diluent_setup(tmp_path: Path):
    with pytest.raises(ValueError, match="four pickup/transfer groups"):
        build_color_campaign(setup(), tmp_path, source_protocol_yaml=SOURCE_PROTOCOL_WATER)


def test_source_steps_rejects_legacy_shape_with_diluent_setup(tmp_path: Path):
    with pytest.raises(ValueError, match="four pickup/transfer groups"):
        build_color_campaign(
            setup(diluent_source="stocks.A4"), tmp_path, source_protocol_yaml=SOURCE_PROTOCOL,
        )


def test_diluent_builder_writes_water_first_and_complete_protocol(tmp_path: Path):
    spec = build_color_campaign(
        setup(diluent_source="stocks.A4"), tmp_path, source_protocol_yaml=SOURCE_PROTOCOL_WATER,
    )
    protocol = yaml.safe_load((tmp_path / spec.protocol_file).read_text())["protocol"]
    assert protocol[1]["transfer"]["source"] == "stocks.A4"
    assert protocol[4]["transfer"]["source"] == "stocks.A1"
    assert protocol[7]["transfer"]["source"] == "stocks.A2"
    assert protocol[10]["transfer"]["source"] == "stocks.A3"
    assert protocol[11]["mix"]["position"] == "plate.A2"
    assert protocol[14]["measure_color"]["reference_lab"] == [42.0, 12.0, 18.0]
    assert spec.objective.path == "14.delta_e_00"
    result = [None] * 14 + [{"delta_e_00": 1.9}]
    assert extract_result_objective(result, spec.objective.path) == 1.9

    names = [parameter.name for parameter in spec.parameters]
    assert names == ["water_ul", "red_ul", "yellow_ul", "blue_ul"]
    bounds = {parameter.name: (parameter.minimum, parameter.maximum) for parameter in spec.parameters}
    assert bounds["water_ul"] == (5.0, 150.0)
    assert bounds["red_ul"] == (50.0, 200.0)
    assert bounds["yellow_ul"] == (50.0, 200.0)
    assert bounds["blue_ul"] == (50.0, 200.0)
    assert spec.sum_constraint.parameters == ["water_ul", "red_ul", "yellow_ul", "blue_ul"]
    assert spec.sum_constraint.total == 300.0
    for point in spec.optimizer.initial_points:
        assert abs(sum(point.values()) - 300.0) < 1e-9

    tip_sequences = {sequence.name: sequence.values for sequence in spec.sequences}
    assert "water_tip" in tip_sequences
    assert tip_sequences["water_tip"][0] != tip_sequences["red_tip"][0]

    # single-trial compile must also work generically for four components
    compiled = compile_trial(
        (tmp_path / spec.protocol_file).read_text(),
        spec.model_dump(),
        {"water_ul": 100.0, "red_ul": 50.0, "yellow_ul": 50.0, "blue_ul": 100.0},
        1,
    )
    compiled_steps = yaml.safe_load(compiled)["protocol"]
    assert compiled_steps[1]["transfer"]["volume_ul"] == 100.0
    assert compiled_steps[10]["transfer"]["volume_ul"] == 100.0
    assert compiled_steps[1]["transfer"]["destination"] == "plate.A3"


def test_diluent_builder_requires_four_tips_per_trial(tmp_path: Path):
    with pytest.raises(ValueError, match="needs 24 available tips"):
        build_color_campaign(
            setup(diluent_source="stocks.A4"), tmp_path,
            available_tip_positions=[f"tips.A{index}" for index in range(1, 20)],
            source_protocol_yaml=SOURCE_PROTOCOL_WATER,
        )


def test_diluent_batch_builder_produces_seventeen_step_template(tmp_path: Path):
    spec = build_color_campaign(
        setup(diluent_source="stocks.A4", batch_size=6), tmp_path,
        source_protocol_yaml=SOURCE_PROTOCOL_WATER,
    )
    protocol = yaml.safe_load((tmp_path / spec.protocol_file).read_text())["protocol"]
    assert len(protocol) == 17
    commands = [next(iter(step)) for step in protocol]
    assert commands == [
        "pick_up_tip", "transfer", "drop_tip",
        "pick_up_tip", "transfer", "drop_tip",
        "pick_up_tip", "transfer", "drop_tip",
        "pick_up_tip", "transfer", "drop_tip",
        "pick_up_tip", "mix", "drop_tip",
        "move", "measure_color",
    ]
    assert protocol[1]["transfer"]["source"] == "stocks.A4"
    assert spec.objective.path == "16.delta_e_00"
    mix_tip_sequence = next(seq for seq in spec.sequences if seq.name == "mix_tip")
    assert mix_tip_sequence.bindings[0].step_index == 12


def test_batch_builder_allocates_77_distinct_tips_for_50_candidates(tmp_path: Path):
    spec = build_color_campaign(
        setup(batch_size=6, candidate_wells=candidate_wells(50)), tmp_path,
        source_protocol_yaml=SOURCE_PROTOCOL,
    )
    tip_sequences = [sequence for sequence in spec.sequences if sequence.name.endswith("_tip")]
    allocated_tips = {tip for sequence in tip_sequences for tip in sequence.values}
    assert len(allocated_tips) == 77
    assert len(next(sequence for sequence in tip_sequences if sequence.name == "mix_tip").values) == 50


def test_diluent_setup_rejects_min_not_multiple_of_five():
    with pytest.raises(ValueError, match="multiple of the 5"):
        setup(diluent_source="stocks.A4", component_min_ul=53.0)


def test_diluent_setup_rejects_infeasible_minimum():
    with pytest.raises(ValueError, match="leaves no room"):
        setup(diluent_source="stocks.A4", component_min_ul=100.0)


def test_diluent_setup_rejects_max_below_min():
    with pytest.raises(ValueError, match="component_max_ul must be"):
        setup(diluent_source="stocks.A4", component_min_ul=60.0, component_max_ul=50.0)


@pytest.mark.parametrize(
    ("candidate_count", "batch_size", "diluent_source", "required_tips"),
    [
        (50, 6, None, 77),
        (55, 6, None, 85),
        (60, 6, None, 90),
        (50, 6, "stocks.A4", 86),
    ],
)
def test_batch_setup_tip_budget_counts_shared_components_and_mix_tips(
    candidate_count, batch_size, diluent_source, required_tips,
):
    parsed = setup(
        batch_size=batch_size,
        diluent_source=diluent_source,
        candidate_wells=candidate_wells(candidate_count),
    )
    assert len(parsed.candidate_wells) == candidate_count
    assert required_tips <= 96


def test_batch_setup_rejects_actual_tip_budget_over_rack_capacity():
    with pytest.raises(ValueError, match="requires 97 fresh tips"):
        setup(
            batch_size=6,
            candidate_wells=candidate_wells(64),
        )


def test_setup_allows_plate_sized_candidate_list_before_tip_budget_validation():
    with pytest.raises(ValueError, match="requires 144 fresh tips"):
        setup(
            batch_size=6,
            target_well="plate.Z0",
            candidate_wells=candidate_wells(95) + ["plate.Z1"],
        )


def test_setup_rejects_more_than_a_96_well_candidate_list():
    with pytest.raises(ValueError, match="at most 96 items"):
        setup(candidate_wells=candidate_wells(95) + ["plate.Z1", "plate.Z2"])


def test_legacy_setup_unaffected_by_default_component_bounds(tmp_path: Path):
    baseline = build_color_campaign(setup(), tmp_path, source_protocol_yaml=SOURCE_PROTOCOL)
    other = build_color_campaign(setup(), tmp_path, source_protocol_yaml=SOURCE_PROTOCOL)
    baseline_protocol = (tmp_path / baseline.protocol_file).read_text()
    other_protocol = (tmp_path / other.protocol_file).read_text()
    assert baseline_protocol == other_protocol
    assert baseline.model_dump(exclude={"protocol_file"}) == other.model_dump(exclude={"protocol_file"})


def test_color_campaign_preset_round_trip_with_diluent():
    from cubos_api.models.campaigns import CampaignPresetDocument, ColorCampaignPresetDraft

    from cubos_api.models.campaigns import CampaignSpec

    setup_obj = setup(diluent_source="stocks.A4", component_min_ul=60.0, component_max_ul=180.0)
    draft = ColorCampaignPresetDraft(
        source_protocol_file=setup_obj.source_protocol_file,
        target_well=setup_obj.target_well,
        red_source=setup_obj.red_source,
        yellow_source=setup_obj.yellow_source,
        blue_source=setup_obj.blue_source,
        diluent_source=setup_obj.diluent_source,
        component_min_ul=setup_obj.component_min_ul,
        component_max_ul=setup_obj.component_max_ul,
        candidate_wells=candidate_wells(50),
    )
    # A preset never carries a fluid_state_id or a built protocol_file/spec state;
    # exercise only the document round trip through JSON, as the API does.

    minimal_spec = CampaignSpec(
        name="draft", gantry_file="g.yaml", deck_file="d.yaml", protocol_file="p.yaml",
        parameters=[{"name": "water_ul", "minimum": 5.0, "maximum": 120.0, "step": 5.0,
                     "bindings": [{"step_index": 1, "argument": "volume_ul"}]}],
    )
    document = CampaignPresetDocument(name="Water dilution", spec=minimal_spec, color_setup=draft)
    round_tripped = CampaignPresetDocument.model_validate_json(document.model_dump_json())
    assert round_tripped.color_setup.diluent_source == "stocks.A4"
    assert round_tripped.color_setup.component_min_ul == 60.0
    assert round_tripped.color_setup.component_max_ul == 180.0
    assert len(round_tripped.color_setup.candidate_wells) == 50
