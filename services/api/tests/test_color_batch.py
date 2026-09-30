from pathlib import Path

import pytest
import yaml

from cubos_api.services.color_batch import compile_color_trial_batch
from cubos_api.services.color_campaign import build_color_campaign
from tests.test_color_campaign import SOURCE_PROTOCOL, SOURCE_PROTOCOL_WATER, setup


def _commands(steps):
    return [next(iter(step)) for step in steps]


def test_legacy_batch_compile_shares_one_tip_per_color_and_one_mix_tip_per_sample(tmp_path: Path):
    spec = build_color_campaign(
        setup(batch_size=6), tmp_path, source_protocol_yaml=SOURCE_PROTOCOL,
    )
    base_protocol = (tmp_path / spec.protocol_file).read_text()
    compiled = compile_color_trial_batch(base_protocol, spec, spec.optimizer.initial_points, 0)
    steps = yaml.safe_load(compiled.protocol_yaml)["protocol"]

    commands = _commands(steps)
    assert commands.count("pick_up_tip") == 3 + 6
    assert commands.count("drop_tip") == 3 + 6
    assert commands.count("mix") == 6
    assert commands.count("move") == 6
    assert commands.count("measure_color") == 6

    assert isinstance(steps[0]["pick_up_tip"]["position"], str)
    assert steps[1]["transfer"]["source"] == "stocks.A1"
    assert steps[9]["transfer"]["source"] == "stocks.A2"
    assert steps[17]["transfer"]["source"] == "stocks.A3"
    destinations = [steps[index]["transfer"]["destination"] for index in (1, 2, 3, 4, 5, 6)]
    assert destinations == [f"plate.A{n}" for n in range(2, 8)]

    assert len(compiled.objective_paths) == 6
    assert len(compiled.sample_map) == 6
    assert compiled.objective_paths[-1].endswith(".delta_e_00")


def test_diluent_batch_compile_places_water_transfers_first_with_one_shared_tip(tmp_path: Path):
    spec = build_color_campaign(
        setup(diluent_source="stocks.A4", batch_size=6), tmp_path,
        source_protocol_yaml=SOURCE_PROTOCOL_WATER,
    )
    base_protocol = (tmp_path / spec.protocol_file).read_text()
    compiled = compile_color_trial_batch(base_protocol, spec, spec.optimizer.initial_points, 0)
    steps = yaml.safe_load(compiled.protocol_yaml)["protocol"]

    commands = _commands(steps)
    assert commands.count("pick_up_tip") == 4 + 6
    assert commands.count("drop_tip") == 4 + 6
    assert commands.count("mix") == 6

    assert commands[0] == "pick_up_tip"
    assert commands[1:7] == ["transfer"] * 6
    assert commands[7] == "drop_tip"
    assert all(steps[index]["transfer"]["source"] == "stocks.A4" for index in range(1, 7))
    water_destinations = [steps[index]["transfer"]["destination"] for index in range(1, 7)]
    assert water_destinations == [f"plate.A{n}" for n in range(2, 8)]

    assert commands[8] == "pick_up_tip"
    assert steps[9]["transfer"]["source"] == "stocks.A1"
    assert commands[16] == "pick_up_tip"
    assert steps[17]["transfer"]["source"] == "stocks.A2"
    assert commands[24] == "pick_up_tip"
    assert steps[25]["transfer"]["source"] == "stocks.A3"

    assert len(compiled.objective_paths) == 6
    assert len(compiled.sample_map) == 6
    for entry, well in zip(compiled.sample_map, [f"plate.A{n}" for n in range(2, 8)]):
        assert entry["candidate_well"] == well


def test_diluent_batch_compile_rejects_legacy_objective_path(tmp_path: Path):
    spec = build_color_campaign(
        setup(diluent_source="stocks.A4", batch_size=6), tmp_path,
        source_protocol_yaml=SOURCE_PROTOCOL_WATER,
    )
    base_protocol = (tmp_path / spec.protocol_file).read_text()
    tampered = spec.model_copy(update={
        "objective": spec.objective.model_copy(update={"path": "13.delta_e_00"}),
    })
    with pytest.raises(ValueError, match="requires a result objective"):
        compile_color_trial_batch(base_protocol, tampered, spec.optimizer.initial_points, 0)


def test_batch_compile_rejects_a_step_shape_matching_neither_layout(tmp_path: Path):
    spec = build_color_campaign(
        setup(batch_size=6), tmp_path, source_protocol_yaml=SOURCE_PROTOCOL,
    )
    malformed = yaml.safe_dump({"protocol": [
        {"pick_up_tip": {"position": "tips.A1"}},
        {"transfer": {"source": "stocks.A1", "destination": "plate.A2", "volume_ul": 100}},
        {"drop_tip": {"position": "waste"}},
        {"move": {"instrument": "camera", "position": "plate.A2"}},
        {"measure_color": {"instrument": "camera", "position": "plate.A2"}},
    ]}, sort_keys=False)
    with pytest.raises(ValueError, match="explicit color-and-dedicated-mix"):
        compile_color_trial_batch(malformed, spec, spec.optimizer.initial_points, 0)


def test_batch_compile_requires_batch_size_greater_than_one(tmp_path: Path):
    spec = build_color_campaign(setup(), tmp_path, source_protocol_yaml=SOURCE_PROTOCOL)
    base_protocol = (tmp_path / spec.protocol_file).read_text()
    with pytest.raises(ValueError, match="batch_size greater than one"):
        compile_color_trial_batch(base_protocol, spec, spec.optimizer.initial_points[:1], 0)
