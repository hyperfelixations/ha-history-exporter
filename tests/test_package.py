from ha_history_exporter import __version__
from ha_history_exporter.manifest import SCHEMA_VERSION, SCRIPT_VERSION


def test_version_constants_are_consistent():
    assert __version__ == "1.3.2"
    assert SCRIPT_VERSION == __version__
    assert SCHEMA_VERSION == "1.3"
