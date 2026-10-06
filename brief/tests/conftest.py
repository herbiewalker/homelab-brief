import pytest

from brief import config


@pytest.fixture(autouse=True)
def no_leftover_config_fallbacks():
    """The config guard's last-good fallbacks are process-wide; one test's must not leak into the next."""
    config.clear_fallbacks()
    yield
    config.clear_fallbacks()
