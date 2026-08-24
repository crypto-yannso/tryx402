"""Planner — price an entire multi-tool plan BEFORE spending anything.

Pattern borrowed from agentpay's `estimate_plan()`: the agent describes a
multi-call workflow, we price every step from known prices (expected or
discovered) and report whether it fits the session budget — with zero wallet
activity. Nothing here can spend.

    gw = Gateway(max_budget_usd=1.0)
    plan = gw.plan([
        ("https://a.dev/api/email/work", {"email": "x@y.com"}, 0.03),
        ("https://b.dev/api/search",     {"q": "geo"},       None),   # unknown price
    ])
    plan["fits_budget"]   # False if total would exceed remaining budget
    plan["total_usd"]     # 0.03 (unknown steps priced at 0 but flagged)

Unknown prices are flagged, never guessed: `unknown` lists steps whose cost
cannot be verified, mirroring the ledger's "-1.0 means unknown" discipline.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class PlanStep:
    """One planned call: (url, body?, expected_price?)."""

    __slots__ = ("url", "body", "price")

    def __init__(self, url, body=None, price=None):
        self.url: str = url
        self.body: dict | None = body
        self.price: float | int | str | None = price

    @classmethod
    def coerce(cls, spec) -> "PlanStep":
        if isinstance(spec, PlanStep):
            return spec
        if isinstance(spec, dict):
            return cls(spec.get("url"), spec.get("body"), spec.get("price"))
        if isinstance(spec, (tuple, list)):
            url = spec[0] if spec else None
            body = spec[1] if len(spec) > 1 else None
            price = spec[2] if len(spec) > 2 else None
            return cls(url, body, price)
        raise TypeError(f"cannot interpret plan step: {spec!r}")


@dataclass
class PlanEstimate:
    steps: list = field(default_factory=list)      # [{url, price_usd, known}]
    total_usd: float = 0.0
    fits_budget: bool = True
    budget_remaining_after: float | None = None
    unknown_count: int = 0
    over_budget_steps: list = field(default_factory=list)  # indices of steps that individually break the cap

    def to_dict(self) -> dict:
        return {
            "steps": self.steps,
            "total_usd": round(self.total_usd, 6),
            "known_total_usd": round(self.total_usd, 6),
            "unknown_count": self.unknown_count,
            "fits_budget": self.fits_budget,
            "budget_remaining_after": (
                round(self.budget_remaining_after, 6)
                if self.budget_remaining_after is not None else None
            ),
            "over_budget_steps": self.over_budget_steps,
        }


def estimate_plan(steps_spec, spent_usd: float = 0.0,
                  max_budget_usd: float | None = None) -> PlanEstimate:
    """Price a list of PlanStep-coercible specs against the session budget.

    Pure function: no network, no payment, no side effects.
    """
    est = PlanEstimate()
    running = float(spent_usd)

    for i, spec in enumerate(steps_spec):
        step = PlanStep.coerce(spec)
        known = step.price is not None
        price = 0.0
        if known and not isinstance(step.price, bool):
            try:
                price = float(step.price)   # raises on garbage strings
            except (TypeError, ValueError):
                known = False
        entry = {
            "index": i,
            "url": step.url,
            "price_usd": round(price, 6) if known else -1.0,   # -1.0 == unknown (ledger convention)
            "known": known,
        }
        est.steps.append(entry)
        if not known:
            est.unknown_count += 1
        est.total_usd += price

        # Per-step circuit check: would THIS step alone push us past the cap?
        if max_budget_usd is not None and running + price > max_budget_usd + 1e-9:
            est.over_budget_steps.append(i)
        running += price

    est.budget_remaining_after = (
        max_budget_usd - running if max_budget_usd is not None else None
    )
    est.fits_budget = (
        max_budget_usd is None
        or (running <= max_budget_usd + 1e-9 and est.unknown_count == 0)
    )
    return est


def suggest_cheaper(steps_spec, spent_usd: float = 0.0,
                    max_budget_usd: float | None = None) -> list:
    """Steps whose removal (or price drop) would make the plan fit.

    Returns actionable suggestions, cheapest-first impact. Unknown-price steps
    are always flagged first: they are the biggest budget risk.
    """
    est = estimate_plan(steps_spec, spent_usd, max_budget_usd)
    if est.fits_budget:
        return []
    out = []
    for s in est.steps:
        if not s["known"]:
            out.append({"index": s["index"], "url": s["url"],
                        "reason": "price_unknown — verify before committing"})
    for i in est.over_budget_steps:
        out.append({"index": i, "url": est.steps[i]["url"],
                    "reason": "step breaks the cap even alone"})
    return out
