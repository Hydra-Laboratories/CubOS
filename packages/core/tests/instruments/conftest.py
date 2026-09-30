"""Shared instrument-test fixtures."""

import pytest

from cubos.instruments.controllers.pawduino import PawduinoLink


@pytest.fixture(autouse=True)
def _reset_pawduino_links():
    """Isolate the per-port PawduinoLink registry between tests.

    The registry is deliberately process-global (drivers configured with the
    same port must share one serial connection), so without this reset a
    link mock-connected in one test would leak into the next.
    """
    PawduinoLink.reset_registry()
    yield
    PawduinoLink.reset_registry()
