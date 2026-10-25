"""``edu09-mcp --port`` validation: port 0 would publish an unusable URL in owner.json."""

import pytest

from edu09.mcp_server import __main__ as cli


@pytest.mark.parametrize("value", ["0", "-1", "65536", "http"])
def test_invalid_port_is_a_usage_error(value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["--http", "--port", value])
    assert exc.value.code == 2


@pytest.mark.parametrize("value", ["1", "8765", "65535"])
def test_valid_port_is_accepted(value: str) -> None:
    assert cli.build_parser().parse_args(["--http", "--port", value]).port == int(value)
