"""Tests for CubOS protocol-run persistence helpers."""

from __future__ import annotations

import pytest

from cubos.data.data_store import DataStore
from cubos.data.protocol_runs import create_campaign_for_protocol_run, register_deck_labware
from cubos.deck import Coordinate3D, Deck, Vial, VialHolder


GANTRY_YAML = """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 90.0
working_volume:
  x_min: 0.0
  x_max: 300.0
  y_min: 0.0
  y_max: 200.0
  z_min: 0.0
  z_max: 80.0
instruments: {}
"""


DECK_YAML = """\
labware:
  vial_holder:
    type: vial_holder
    name: panda_vial_holder
    location:
      x: 17.1
      y: 132.9
      z: 20.0
    vials:
      vial_1:
        model_name: 20ml_vial
        height: 57.0
        diameter: 28.0
        location:
          x: 17.1
          y: 0.9
        capacity_ul: 20000.0
        working_volume_ul: 12000.0
"""


def _vial(name: str, x: float) -> Vial:
    return Vial(
        name=name,
        model_name="programmatic_vial",
        height=40.0,
        diameter=10.0,
        location=Coordinate3D(x=x, y=20.0, z=30.0),
        capacity_ul=1000.0,
        working_volume_ul=900.0,
    )


def _nested_holder() -> VialHolder:
    return VialHolder(
        name="programmatic_holder",
        location=Coordinate3D(x=5.0, y=6.0, z=7.0),
        contained_labware={"nested": _vial("nested", 10.0)},
    )


def test_programmatic_mixed_deck_registers_direct_and_nested_labware() -> None:
    deck = Deck({
        "direct": _vial("direct", 40.0),
        "holder": _nested_holder(),
    })
    assert deck.has_explicit_volume_registry is False

    store = DataStore(db_path=":memory:")
    campaign_id = store.create_campaign("programmatic mixed deck")
    register_deck_labware(store, campaign_id, deck)

    rows = store._conn.execute(
        "SELECT labware_key, labware_type, well_id FROM labware "
        "WHERE campaign_id = ? ORDER BY labware_key",
        (campaign_id,),
    ).fetchall()
    store.close()

    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("direct", "vial", None),
        ("holder.nested", "vial", None),
    ]


def test_create_campaign_for_protocol_run_validates_before_creating_campaign(tmp_path):
    gantry_path = tmp_path / "gantry.yaml"
    deck_path = tmp_path / "missing_deck.yaml"
    gantry_path.write_text(GANTRY_YAML, encoding="utf-8")
    store = DataStore(db_path=":memory:")

    with pytest.raises(Exception):
        create_campaign_for_protocol_run(
            store,
            gantry_path=gantry_path,
            deck_path=deck_path,
            gantry_file="gantry.yaml",
            deck_file="missing_deck.yaml",
            protocol_file="protocol.yaml",
        )

    count = store._conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    store.close()
    assert count == 0
