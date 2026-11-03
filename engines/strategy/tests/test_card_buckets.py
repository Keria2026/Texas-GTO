import json
from itertools import combinations, permutations
from random import Random

import pytest

import texas_strategy.card_buckets as buckets
from texas_game_core.cards import DECK


def test_preflop_has_169_classes_and_postflop_features_are_suit_and_card_order_invariant():
    assert len({buckets.card_bucket(pair, ()) for pair in combinations(DECK, 2)}) == 169
    hole, board = ("As", "Js"), ("Ks", "Qs", "2d", "3h", "4c")
    for length in (3, 4, 5):
        expected = buckets.card_features(hole, board[:length])
        label = buckets.card_bucket(hole, board[:length])
        assert len(expected) == buckets.FEATURE_COUNT and all(0 <= value <= 1 for value in expected)
        assert label[0] == length and 0 <= label[1] < 200
        for suits in permutations("cdhs"):
            mapping = dict(zip("cdhs", suits, strict=True))
            remap = lambda cards, mapping=mapping: tuple(c[0] + mapping[c[1]] for c in cards)  # noqa: E731
            assert buckets.card_features(remap(hole[::-1]), remap(board[:length][::-1])) == expected
            assert buckets.card_bucket(remap(hole), remap(board[:length])) == label


def test_future_cards_are_not_inputs_to_an_earlier_bucket_and_draws_are_distinguished():
    before = buckets.card_bucket(("As", "Js"), ("Ks", "Qs", "2d"))
    for turn in ("Ts", "2c", "9h"):
        buckets.card_bucket(("As", "Js"), ("Ks", "Qs", "2d", turn))
        assert buckets.card_bucket(("As", "Js"), ("Ks", "Qs", "2d")) == before
    assert buckets.card_features(("As", "Js"), ("Ks", "Qs", "2d")) != buckets.card_features(
        ("Ah", "Jc"), ("Ks", "Qs", "2d")
    )
    with pytest.raises(ValueError, match="complete street"):
        buckets.card_bucket(("As", "Ks"), ("2d",))
    with pytest.raises(ValueError, match="duplicate"):
        buckets.card_bucket(("As", "Ks"), ("2d", "3h", "As"))


def test_packaged_clusters_have_held_out_compression_evidence_and_detect_corruption(
    tmp_path, monkeypatch
):
    digest, centers = buckets.codebook()
    assert len(digest) == 64 and all(len(rows) == 200 for rows in centers.values())
    payload = json.loads(buckets.CODEBOOK.read_text())
    for diagnostic in payload["diagnostics"].values():
        assert diagnostic["occupied_training_clusters"] == 200
        assert (
            diagnostic["holdout_mean_squared_distance"]
            < diagnostic["single_centroid_holdout_distance"]
        )
    payload["checksum"] = "0" * 64
    bad = tmp_path / "damaged.json"
    bad.write_text(json.dumps(payload))
    try:
        with monkeypatch.context() as changed:
            changed.setattr(buckets, "CODEBOOK", bad)
            buckets.codebook.cache_clear()
            with pytest.raises(ValueError, match="damaged"):
                buckets.codebook()
    finally:
        buckets.codebook.cache_clear()


def test_bucket_model_is_deterministic_on_independent_legal_cards():
    rng = Random(17)
    for size in (3, 4, 5):
        for _ in range(25):
            cards = rng.sample(DECK, size + 2)
            first = buckets.card_bucket(tuple(cards[:2]), tuple(cards[2:]))
            buckets._postflop_bucket.cache_clear()
            assert buckets.card_bucket(tuple(cards[:2]), tuple(cards[2:])) == first
