
from cubos.deck import BoundingBoxGeometry, Coordinate3D, TipRack


def test_tip_rack_exposes_shared_bounding_box_geometry():
    rack = TipRack(
        name="tip_rack_a",
        model_name="test_tip_rack",
        rows=2,
        columns=2,
        pickup_z=191.0,
        drop_z=174.8,
        tip_length=59.3,
        tips={
            "A1": Coordinate3D(x=111.9, y=2.7, z=191.0),
            "A2": Coordinate3D(x=111.9, y=11.2, z=191.0),
            "B1": Coordinate3D(x=120.4, y=2.7, z=191.0),
            "B2": Coordinate3D(x=120.4, y=11.2, z=191.0),
        },
    )

    assert rack.geometry == BoundingBoxGeometry(
        length=8.5,
        width=8.5,
        height=16.2,
    )
    assert rack.get_initial_position() == Coordinate3D(x=111.9, y=2.7, z=191.0)
