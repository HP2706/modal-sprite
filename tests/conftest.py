import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip integration tests unless ``--run-integration`` is passed."""
    if config.getoption("--run-integration", default=False):
        return
    skip_marker = pytest.mark.skip(reason="needs --run-integration flag")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that require a live Modal connection",
    )
