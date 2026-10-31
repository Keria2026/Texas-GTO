"""Observable-history profiles and an optional, non-equilibrium expert layer.

The Pluribus baseline does not depend on this module. Opportunity definitions,
context buckets and action-conditioned hand likelihoods are caller inputs; VPIP
or a style label alone cannot identify a player's private-card range. This code
does not turn a posterior or a template distance into a measured action EV.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import exp, fsum, isclose, isfinite, log, sqrt

MODEL_VERSION = "observable-opponent-v1"
HoleCards = tuple[str, str]


def _name(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")


def _count(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


def _positive(value: float, label: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")


@dataclass(frozen=True)
class ScopedPlayer:
    """Stable identity within a table session; display names are not identifiers."""

    session_id: str
    table_id: str
    player_id: str

    def __post_init__(self) -> None:
        for field in ("session_id", "table_id", "player_id"):
            _name(getattr(self, field), field)


@dataclass(frozen=True)
class ProfileEvent:
    """One explicitly identified opportunity or one public action observation.

    observed_at and available_at are nonnegative logical timestamps in the
    caller's replay clock. available_at must be >= observed_at. A hand-level
    statistic finalized at hand end becomes available only then, not at the
    earlier decision. Events carry no hidden cards or inferred showdown labels.
    For opportunity events, eligible=False contributes to neither count.
    For action events, legal_actions is the exact available action support.
    """

    event_id: str
    player: ScopedPlayer
    context: str
    observed_at: int
    kind: str
    name: str = ""
    success: bool | None = None
    eligible: bool = True
    action: str | None = None
    legal_actions: tuple[str, ...] = ()
    available_at: int | None = None

    def __post_init__(self) -> None:
        _name(self.event_id, "event_id")
        _name(self.context, "context")
        if not isinstance(self.player, ScopedPlayer):
            raise ValueError("player must be an explicitly scoped identity")
        _count(self.observed_at, "observed_at")
        if self.available_at is not None:
            _count(self.available_at, "available_at")
            if self.available_at < self.observed_at:
                raise ValueError("available_at cannot precede the observation")
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be a boolean")
        if self.kind == "opportunity":
            _name(self.name, "statistic name")
            if not isinstance(self.success, bool):
                raise ValueError("An opportunity requires an explicit boolean success")
            if self.success and not self.eligible:
                raise ValueError("A success must have an eligible opportunity")
            if self.action is not None or self.legal_actions:
                raise ValueError("Opportunity events cannot also be action observations")
        elif self.kind == "action":
            _validate_actions(self.legal_actions)
            if self.action not in self.legal_actions or not self.eligible:
                raise ValueError("Action must be legal at an eligible decision")
            if self.success is not None or self.name:
                raise ValueError("Action events cannot also be opportunity statistics")
            object.__setattr__(self, "legal_actions", tuple(sorted(self.legal_actions)))
        else:
            raise ValueError("kind must be 'opportunity' or 'action'")

    @property
    def known_at(self) -> int:
        return self.observed_at if self.available_at is None else self.available_at


def _validate_actions(actions: Sequence[str]) -> None:
    if not actions or len(set(actions)) != len(actions):
        raise ValueError("Require a nonempty distinct legal action support")
    for action in actions:
        _name(action, "action")


@dataclass(frozen=True)
class BetaPosterior:
    successes: int
    opportunities: int
    alpha: float
    beta: float
    model_version: str = MODEL_VERSION
    window_start: int = 0
    as_of: int | None = None
    interval_method: str = "chebyshev_posterior_mass_bound"

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        total = self.alpha + self.beta
        return self.mean * (1 - self.mean) / (total + 1)

    def interval(self, mass: float = 0.95) -> tuple[float, float]:
        """Conservative posterior mass bound via Chebyshev, not a central quantile.

        This dependency-free interval contains at least ``mass`` of the Beta
        posterior. It can be much wider than an equal-tailed credible interval.
        """
        if not isfinite(mass) or not 0 < mass < 1:
            raise ValueError("mass must lie strictly between zero and one")
        radius = sqrt(self.variance / (1 - mass))
        return max(0.0, self.mean - radius), min(1.0, self.mean + radius)


@dataclass(frozen=True)
class ActionPosterior:
    counts: Mapping[str, int]
    concentrations: Mapping[str, float]
    probabilities: Mapping[str, float]
    observations: int
    model_version: str = MODEL_VERSION
    window_start: int = 0
    as_of: int | None = None


class OpponentModel:
    """Rebuildable statistics; deduplication is scoped by player and event ID.

    Historical queries require as_of, so a caller must declare the information
    cutoff. since limits *observation* time, and as_of also filters availability.
    Corrections replace the event log with rebuild; they do not increment a
    previously counted observation. Storage/persistence belongs to the caller.
    """

    def __init__(self, events: Iterable[ProfileEvent] = ()) -> None:
        self._events: dict[tuple[ScopedPlayer, str], ProfileEvent] = {}
        for event in events:
            self.record(event)

    @property
    def events(self) -> tuple[ProfileEvent, ...]:
        return tuple(self._events.values())

    def record(self, event: ProfileEvent) -> bool:
        if not isinstance(event, ProfileEvent):
            raise ValueError("A validated ProfileEvent is required")
        key = event.player, event.event_id
        previous = self._events.get(key)
        if previous is not None:
            if previous != event:
                raise ValueError("Conflicting payload for an existing scoped event_id")
            return False
        self._events[key] = event
        return True

    def rebuild(self, events: Iterable[ProfileEvent]) -> None:
        candidate = OpponentModel(events)
        self._events = candidate._events

    def _visible(
        self, player: ScopedPlayer, context: str, as_of: int, since: int
    ) -> Iterable[ProfileEvent]:
        _count(as_of, "as_of")
        _count(since, "since")
        if since > as_of:
            raise ValueError("since cannot be later than as_of")
        return (
            event
            for event in self._events.values()
            if event.player == player
            and event.context == context
            and since <= event.observed_at <= as_of
            and event.known_at <= as_of
        )

    def stat(
        self,
        player: ScopedPlayer,
        context: str,
        name: str,
        *,
        as_of: int,
        since: int = 0,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
    ) -> BetaPosterior:
        _positive(prior_alpha, "prior_alpha")
        _positive(prior_beta, "prior_beta")
        if not isfinite(prior_alpha + prior_beta):
            raise ValueError("Beta prior concentration is too large")
        events = [
            event
            for event in self._visible(player, context, as_of, since)
            if event.kind == "opportunity" and event.name == name and event.eligible
        ]
        successes = sum(event.success for event in events)
        return BetaPosterior(
            successes,
            len(events),
            prior_alpha + successes,
            prior_beta + len(events) - successes,
            window_start=since,
            as_of=as_of,
        )

    def actions(
        self,
        player: ScopedPlayer,
        context: str,
        legal_actions: Sequence[str],
        *,
        as_of: int,
        since: int = 0,
        prior: Mapping[str, float] | None = None,
    ) -> ActionPosterior:
        """Dirichlet posterior for this context and exactly this legal support.

        A check/bet observation is not counted in a fold/call/raise node. A
        caller may use finer amount/history buckets in context; no automatic
        similarity or off-tree action mapping is performed here.
        """
        _validate_actions(legal_actions)
        support = tuple(sorted(legal_actions))
        alpha = dict.fromkeys(support, 1.0) if prior is None else dict(prior)
        if set(alpha) != set(support):
            raise ValueError("Dirichlet prior must match the legal action support")
        for value in alpha.values():
            _positive(value, "Dirichlet concentration")
        counts = dict.fromkeys(support, 0)
        for event in self._visible(player, context, as_of, since):
            if event.kind == "action" and event.legal_actions == support:
                counts[event.action] += 1
        posterior = {action: alpha[action] + counts[action] for action in support}
        try:
            total = fsum(posterior.values())
        except OverflowError as error:
            raise ValueError("Dirichlet concentration is too large") from error
        if not isfinite(total):
            raise ValueError("Dirichlet concentration is too large")
        return ActionPosterior(
            counts,
            posterior,
            {action: posterior[action] / total for action in support},
            sum(counts.values()),
            window_start=since,
            as_of=as_of,
        )


def _card(card: str) -> None:
    if not isinstance(card, str) or len(card) != 2 or card[0] not in "23456789TJQKA":
        raise ValueError("Cards use rank/suit tokens such as 'As' and 'Td'")
    if card[1] not in "cdhs":
        raise ValueError("Card suit must be one of c, d, h, s")


def _range_keys(values: Mapping[HoleCards, float], name: str) -> dict[HoleCards, float]:
    canonical = {}
    for hand, value in values.items():
        if not isinstance(hand, tuple) or len(hand) != 2 or hand[0] == hand[1]:
            raise ValueError("Each range key must be a tuple of two distinct card tokens")
        for card in hand:
            _card(card)
        key = tuple(sorted(hand))
        if key in canonical:
            raise ValueError("Duplicate unordered hand combination")
        if not isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
        canonical[key] = value
    return canonical


def bayesian_range_update(
    prior: Mapping[HoleCards, float],
    likelihoods: Mapping[HoleCards, float],
    known_cards: Sequence[str] = (),
) -> dict[HoleCards, float]:
    """P(hand|public action) proportional to prior(hand) * P(action|hand, context).

    Remove hero/public-card blockers before normalizing. Likelihoods must come
    from a separately specified action-conditioned model available at the
    decision time, not later showdown cards or an unconditional VPIP frequency.
    We return canonical, unordered card pairs; impossible evidence is rejected.
    """
    for card in known_cards:
        _card(card)
    if len(set(known_cards)) != len(known_cards):
        raise ValueError("Known cards contain duplicates")
    weights = _range_keys(prior, "Prior weights")
    likelihood = _range_keys(likelihoods, "Action likelihoods")
    if any(value > 1 for value in likelihood.values()):
        raise ValueError("Action likelihoods must lie between zero and one")
    blocked = set(known_cards)
    log_weights = {}
    for hand, weight in weights.items():
        if weight == 0 or blocked.intersection(hand):
            continue
        if hand not in likelihood:
            raise ValueError("Missing action likelihood for a possible hand")
        if likelihood[hand] > 0:
            log_weights[hand] = log(weight) + log(likelihood[hand])
    if not log_weights:
        raise ValueError("No possible range remains: blockers or zero-likelihood evidence")
    peak = max(log_weights.values())
    posterior = {hand: exp(weight - peak) for hand, weight in log_weights.items()}
    total = fsum(posterior.values())
    return {hand: weight / total for hand, weight in posterior.items()}


@dataclass(frozen=True)
class ExpertBlend:
    probabilities: Mapping[str, float]
    expert_weights: Mapping[str, float]
    adaptation_weight: float
    source: str
    equilibrium_guarantee: bool = False


def _distribution(values: Mapping[str, float]) -> dict[str, float]:
    _validate_actions(tuple(values))
    if any(not isfinite(value) or value < 0 or value > 1 for value in values.values()):
        raise ValueError("Action probabilities must lie between zero and one")
    total = fsum(values.values())
    if not isclose(total, 1.0, abs_tol=1e-9, rel_tol=1e-9):
        raise ValueError("Action probabilities must sum to one")
    return {action: value / total for action, value in values.items()}


def blend_experts(
    baseline: Mapping[str, float],
    experts: Mapping[str, Mapping[str, float]],
    distances: Mapping[str, float | None],
    *,
    confidence: float,
    sample_count: int = 0,
    minimum_samples: int = 20,
    temperature: float = 1.0,
    maximum_weight: float = 0.25,
) -> ExpertBlend:
    """Experimental negative-distance softmax with a caller-supplied confidence gate.

    This is a project extension, not Pluribus or a reproduced paper formula.
    Missing template paths have distance=None and receive no mass. Low sample
    support, zero confidence or no covered expert returns the baseline. The
    gate is min(confidence, maximum_weight); it is an engineering cap, not a
    worst-case utility bound or a guarantee of positive exploitation value.
    The caller must independently evaluate any mixture before recommending it.
    """
    base = _distribution(baseline)
    _count(sample_count, "sample_count")
    _count(minimum_samples, "minimum_samples")
    _positive(temperature, "temperature")
    for value in (confidence, maximum_weight):
        if not isfinite(value) or not 0 <= value <= 1:
            raise ValueError("confidence and maximum_weight must lie between zero and one")
    if set(experts) != set(distances):
        raise ValueError("Provide a distance or None for each expert")
    covered = {}
    policies = {}
    for name, expert in experts.items():
        _name(name, "expert name")
        policies[name] = _distribution(expert)
        if set(policies[name]) != set(base):
            raise ValueError("Expert actions must match the baseline legal support exactly")
        distance = distances[name]
        if distance is not None:
            if not isfinite(distance) or distance < 0:
                raise ValueError("Template distances must be finite and nonnegative")
            covered[name] = distance
    gate = min(confidence, maximum_weight) if sample_count >= minimum_samples else 0.0
    if not covered or gate == 0:
        return ExpertBlend(base, {}, 0.0, "baseline")
    closest = min(covered.values())
    weights = {name: exp(-(distance - closest) / temperature) for name, distance in covered.items()}
    total = fsum(weights.values())
    weights = {name: value / total for name, value in weights.items()}
    mixed = {
        action: (1 - gate) * base[action]
        + gate * fsum(weight * policies[name][action] for name, weight in weights.items())
        for action in base
    }
    return ExpertBlend(_distribution(mixed), weights, gate, "experimental_profile_mix")
