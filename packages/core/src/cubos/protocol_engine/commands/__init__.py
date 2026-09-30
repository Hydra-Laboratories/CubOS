"""Protocol commands package.

Importing this package triggers all @protocol_command decorators,
populating the CommandRegistry.
"""

from . import camera, capper, cure, home, lights, measure, move, pause, pipette, rinse, scan  # noqa: F401 -- side-effect imports for registration

__all__ = [
    "camera",
    "capper",
    "cure",
    "home",
    "lights",
    "measure",
    "move",
    "pause",
    "pipette",
    "rinse",
    "scan",
]
