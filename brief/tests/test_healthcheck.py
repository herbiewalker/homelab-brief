from brief import healthcheck


def test_check_fails_when_nothing_is_listening():
    # Port 1 is reserved and never serves HTTP, so the check must report unhealthy.
    assert healthcheck.check("1") is False
