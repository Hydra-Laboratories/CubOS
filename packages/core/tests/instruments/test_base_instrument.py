import pytest
from cubos.instruments.base_instrument import BaseInstrument, InstrumentError


class MockInstrument(BaseInstrument):
    def __init__(
        self,
        name="mock_instrument",
        offset_x=0.0,
        offset_y=0.0,
        depth=0.0,
    ):
        super().__init__(
            name=name,
            offset_x=offset_x,
            offset_y=offset_y,
            depth=depth,
        )
        self.connected = False
        self.healthy = True

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def health_check(self):
        return self.healthy


class BadInstrument(BaseInstrument):
    pass


def test_concrete_implementation():
    instr = MockInstrument()
    assert instr.name == "mock_instrument"

    instr.connect()
    assert instr.connected is True

    instr.disconnect()
    assert instr.connected is False

    assert instr.health_check() is True


def test_handle_error_wraps_exception():
    instr = MockInstrument()
    original_error = ValueError("Something went wrong")

    with pytest.raises(InstrumentError) as exc_info:
        instr.handle_error(original_error, "testing context")

    assert "Something went wrong" in str(exc_info.value)
    assert "testing context" in str(exc_info.value)
    assert exc_info.value.__cause__ is original_error


def test_handle_error_passes_through_instrument_error():
    instr = MockInstrument()
    original_error = InstrumentError("Already an instrument error")

    with pytest.raises(InstrumentError) as exc_info:
        instr.handle_error(original_error, "processing")

    assert exc_info.value is original_error
