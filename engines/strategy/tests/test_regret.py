import math

import pytest

from texas_strategy import PluribusStyleProvider, regret_matching


def test_positive_regrets_define_the_distribution():
    assert regret_matching([-4, 3, 1]) == (0.0, 0.75, 0.25)


def test_without_positive_regret_every_legal_action_is_available():
    assert regret_matching([-2, 0, -1]) == pytest.approx((1 / 3,) * 3)
    assert regret_matching([0]) == (1.0,)


def test_large_regrets_and_scale_invariance():
    assert regret_matching([1e308, 1e308, -1e308]) == (0.5, 0.5, 0.0)
    assert regret_matching([1e-200, 3e-200]) == pytest.approx((0.25, 0.75))


@pytest.mark.parametrize("values", [[], [math.nan], [math.inf], [-math.inf]])
def test_invalid_regrets_are_rejected(values):
    with pytest.raises(ValueError):
        regret_matching(values)


def test_provider_does_not_claim_unbuilt_capabilities():
    provider = PluribusStyleProvider()
    assert not provider.coverage("standard-holdem-6max-v1").supported
    assert "blueprint_not_available" in provider.coverage("kuhn-v1").reasons
    assert "rules_version_missing" in provider.coverage("").reasons
