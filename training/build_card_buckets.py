"""Rebuild the deterministic synthetic postflop codebook; no private/live data."""

import json
from hashlib import sha256
from random import Random

from texas_game_core.cards import DECK
from texas_strategy.card_buckets import (
    CODEBOOK,
    FEATURE_COUNT,
    FEATURE_VERSION,
    card_features,
    nearest,
    squared_distance,
)


def build():
    rng = Random(20261101)
    result = {
        "feature_version": FEATURE_VERSION,
        "seed": 20261101,
        "training_rows_per_street": 4096,
        "holdout_rows_per_street": 1024,
        "epochs": 6,
        "centroids": {},
        "diagnostics": {},
    }
    for size in (3, 4, 5):
        rows = []
        for _ in range(5120):
            cards = rng.sample(DECK, size + 2)
            rows.append(card_features(cards[:2], cards[2:]))
        train, holdout = rows[:4096], rows[4096:]
        # k-means++ seed selection, retaining distances rather than rescanning old centers.
        centers = [train[rng.randrange(len(train))]]
        distances = [squared_distance(row, centers[0]) for row in train]
        while len(centers) < 200:
            center = rng.choices(train, weights=distances, k=1)[0]
            centers.append(center)
            distances = [
                min(old, squared_distance(row, center))
                for row, old in zip(train, distances, strict=True)
            ]
        for _ in range(result["epochs"]):
            totals = [[0.0] * FEATURE_COUNT for _ in centers]
            counts = [0] * len(centers)
            for row in train:
                index = nearest(row, centers)
                counts[index] += 1
                for column, value in enumerate(row):
                    totals[index][column] += value
            centers = [
                tuple(value / count for value in total) if count else old
                for total, count, old in zip(totals, counts, centers, strict=True)
            ]
        mean = tuple(sum(row[j] for row in train) / len(train) for j in range(FEATURE_COUNT))
        held_error = sum(
            squared_distance(row, centers[nearest(row, centers)]) for row in holdout
        ) / len(holdout)
        baseline = sum(squared_distance(row, mean) for row in holdout) / len(holdout)
        assert held_error < baseline and all(counts)
        result["centroids"][str(size)] = centers
        result["diagnostics"][str(size)] = {
            "holdout_mean_squared_distance": held_error,
            "single_centroid_holdout_distance": baseline,
            "occupied_training_clusters": sum(count > 0 for count in counts),
        }
        print(json.dumps({"board_cards": size, **result["diagnostics"][str(size)]}), flush=True)
    checksum = sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    CODEBOOK.write_text(json.dumps({**result, "checksum": checksum}, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    build()
