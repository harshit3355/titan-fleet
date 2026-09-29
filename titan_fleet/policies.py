"""Routing + scaling policies: four baselines, the S3 controller and its ablations.

Every policy sees the same information: current prices, its own metered token usage per managed
quota (as LiteLLM's usage-based filtering does), each pool's queue, shared endpoint cooldowns after
errors, and the latencies it observes. None sees future arrivals, outages or latency noise.
"""
from __future__ import annotations

import math

from .sim import SLOS, TICK, Request, Sim, pool_service


def p_late(median: float, sigma: float, budget: float) -> float:
    """P(lognormal(median, sigma) > budget)."""
    if budget <= 0:
        return 1.0
    return 0.5 * math.erfc(math.log(budget / median) / (sigma * math.sqrt(2)))


class Policy:
    name = "base"

    def __init__(self, sim: Sim):
        self.sim = sim
        self.ttft = {n: e.ttft for n, e in sim.eps.items()}  # EWMA of observed TTFT per endpoint

    def usable(self, r: Request, now: float) -> list[str]:
        s, need = self.sim, r.tin + r.tout
        ok = [n for n, e in s.eps.items()
              if s.cool[n] <= now and (e.kind != "managed" or s.headroom(n, now) >= need)]
        return ok or list(s.eps)

    def route(self, r: Request, now: float) -> list[str]:
        return self.order(r, now, self.usable(r, now))

    def order(self, r: Request, now: float, names: list[str]) -> list[str]:
        return names

    def observe(self, r: Request, outcome) -> None:
        if outcome:
            self.ttft[outcome[0]] += 0.1 * (outcome[3] - self.ttft[outcome[0]])

    def tick(self, now: float) -> None:
        pass


class HPA:
    """Kubernetes-HPA-style reactive scaling of one pool on its own utilisation: target 70% of slots,
    10% tolerance, scale up immediately, scale down to the highest recommendation of the last 5 min."""
    TARGET, TOLERANCE, WINDOW = 0.7, 0.1, 300.0

    def __init__(self, sim: Sim):
        self.sim, self.recs = sim, {n: [] for n in sim.pools}

    def tick(self, now: float) -> None:
        for n, p in self.sim.pools.items():
            cur, busy = p.size(), p.outstanding(now)
            ratio = busy / (max(cur, 1) * p.e.slots) / self.TARGET
            desired = cur if cur and abs(ratio - 1) <= self.TOLERANCE else math.ceil(busy / (p.e.slots * self.TARGET))
            recs = self.recs[n] = [x for x in self.recs[n] if x[0] > now - self.WINDOW] + [(now, desired)]
            p.scale_to(desired if desired >= cur else min(cur, max(d for _, d in recs)), now)


class Autoscaled(Policy):
    """Baselines: a routing rule plus per-pool HPA, no global controller."""

    def __init__(self, sim: Sim):
        super().__init__(sim)
        self.hpa = HPA(sim)

    def tick(self, now: float) -> None:
        self.hpa.tick(now)


class RoundRobin(Autoscaled):
    name = "round_robin"
    i = 0

    def order(self, r, now, names):
        self.i += 1
        k = self.i % len(names)
        return names[k:] + names[:k]


class Cheapest(Autoscaled):
    """Cheapest list price first; a pool's price is its GPU cost per request at full utilisation."""
    name = "cheapest"

    def order(self, r, now, names):
        s = self.sim

        def price(n):
            e = s.eps[n]
            if e.kind == "pool":
                return pool_service(e, r, noise=False)[1] / e.slots * e.gpu_hour / 3600
            return s.request_cost(n, r, now)
        return sorted(names, key=price)


class LatencyOnly(Autoscaled):
    name = "latency_only"

    def order(self, r, now, names):
        return sorted(names, key=lambda n: self.ttft[n])


class PerProviderAutoscale(Autoscaled):
    """Static primary + failover chain in declared order (self-hosted first); each provider scales or
    throttles on its own."""
    name = "per_provider_autoscale"


class TieredStatic(Autoscaled):
    """Extra, stronger baseline (not in the build contract): class-aware static routing, as a LiteLLM
    model-group-per-tier config would express it. Interactive goes to the fastest managed endpoints
    first and the pool last; batch and background go cheapest first."""
    name = "tiered_static"

    def order(self, r, now, names):
        if r.cls == "interactive":
            return sorted(names, key=lambda n: (self.sim.eps[n].kind == "pool", self.sim.eps[n].ttft))
        return Cheapest.order(self, r, now, names)


class S3(Policy):
    """SLO Service Share controller.

    Slow planner (every tick): forecast the pool-worthy demand in concurrent slots (Little's law over
    arrivals) with Holt's linear smoothing, look ahead one cold start, and provision for it.
    Fast router (every request): score = marginal $ + lambda_c * P(SLO miss), where lambda_c is the
    class's penalty times the estimated probability that the class overruns its error budget: while
    the budget is safe, misses are nearly free and cheap-but-late routes win; as it burns, lambda_c
    rises to the full penalty. Batch and background may not spend the last RESERVE of a managed
    quota while interactive's budget is at risk (quota-aware spillover).
    """
    name = "s3"
    BUDGET, PROACTIVE, TREND = True, True, True
    U_TARGET, ALPHA, BETA, RESERVE, PRIOR, SPEND, WINDOW = 0.8, 0.3, 0.1, 0.25, 100, 0.5, 300.0

    def __init__(self, sim: Sim):
        super().__init__(sim)
        self.n = {c: 0 for c in SLOS}
        self.viol = {c: 0 for c in SLOS}
        self.demand = {n: 0.0 for n in sim.pools}
        self.level = {n: None for n in sim.pools}
        self.trend = {n: 0.0 for n in sim.pools}
        self.hpa = None if self.PROACTIVE else HPA(sim)
        self.wants = {n: [] for n in sim.pools}

    def violation_price(self, cls: str, now: float) -> float:
        """$ value of one more violation of `cls`: its penalty x P(the class exceeds SPEND of its error budget
        by the end of the SLO window); the unplanned rest is kept for incidents. The window is declared
        configuration (here the run length), not foresight. The violation rate carries a prior of PRIOR
        requests at the planned rate."""
        slo = SLOS[cls]
        if not self.BUDGET:
            return slo.penalty
        n, allowed = self.n[cls], (1 - slo.objective) * self.SPEND
        total = n * self.sim.scn.duration / max(now, 60.0)
        rate = (self.viol[cls] + allowed * self.PRIOR) / (n + self.PRIOR)
        rem = max(0.0, total - n)
        z = (self.viol[cls] + rate * rem - allowed * total) / math.sqrt(rate * (1 - rate) * rem + 1)
        return slo.penalty * 0.5 * math.erfc(-z / math.sqrt(2))

    def _managed_median(self, n: str) -> float:
        return self.ttft[n] / math.exp(self.sim.eps[n].sigma ** 2 / 2)

    def _pool_worthy(self, r: Request, now: float, e) -> bool:
        """Is this request cheaper on the pool at target utilisation than on any SLO-feasible managed endpoint?"""
        s, slo = self.sim, SLOS[r.cls]
        pool_cost = pool_service(e, r, noise=False)[1] / e.slots * e.gpu_hour / 3600 / self.U_TARGET
        alt = [s.request_cost(n, r, now) for n, m in s.eps.items() if m.kind == "managed"
               and self._late(n, r, now) <= 1 - slo.objective]
        return not alt or pool_cost < min(alt)

    def _late(self, n: str, r: Request, now: float) -> float:
        s, slo = self.sim, SLOS[r.cls]
        e = s.eps[n]
        if e.kind == "pool":
            ttft, total = pool_service(e, r, noise=False)
            est = s.pools[n].wait(now) + (ttft if slo.metric == "ttft" else total)
            return 1.0 if est > slo.target else 0.0
        med = self._managed_median(n)
        if slo.metric == "e2e":
            med += r.tout * e.tpot * med / e.ttft
        return p_late(med, e.sigma, slo.target)

    def order(self, r, now, names):
        s, slo = self.sim, SLOS[r.cls]
        lam = self.violation_price(r.cls, now)
        reserve = self.RESERVE * min(1.0, 2 * self.violation_price("interactive", now) / SLOS["interactive"].penalty)
        scored, held = [], []
        for n in names:
            e = s.eps[n]
            if e.kind == "managed":
                if r.cls != "interactive" and s.headroom(n, now) - r.tin - r.tout < reserve * s.quota(n, now):
                    held.append(n)
                    continue
                cost = s.request_cost(n, r, now)
            else:
                cost = 0.0  # capacity already paid for; the planner owns the pool's cost
            scored.append((cost + lam * self._late(n, r, now), n))
        return [n for _, n in sorted(scored)] + held

    def route(self, r, now):
        for n, p in self.sim.pools.items():
            if self._pool_worthy(r, now, p.e):
                self.demand[n] += pool_service(p.e, r, noise=False)[1]
        return super().route(r, now)

    def observe(self, r, outcome):
        super().observe(r, outcome)
        slo = SLOS[r.cls]
        self.n[r.cls] += 1
        self.viol[r.cls] += outcome is None or outcome[1 if slo.metric == "ttft" else 2] > slo.target

    def tick(self, now):
        if self.hpa:
            self.hpa.tick(now)
            for n in self.demand:
                self.demand[n] = 0.0
            return
        for n, p in self.sim.pools.items():
            x, self.demand[n] = self.demand[n] / TICK, 0.0  # concurrent slots needed (Little's law)
            if self.level[n] is None:
                self.level[n] = x
            else:
                prev = self.level[n]
                self.level[n] = self.ALPHA * x + (1 - self.ALPHA) * (prev + self.trend[n])
                self.trend[n] = self.BETA * (self.level[n] - prev) + (1 - self.BETA) * self.trend[n]
            ahead = self.level[n] + (p.e.cold_start + TICK) / TICK * self.trend[n] * self.TREND
            want = math.ceil(max(ahead, p.outstanding(now)) / (p.e.slots * self.U_TARGET))
            cur = p.size()
            wants = self.wants[n] = [w for w in self.wants[n] if w[0] > now - self.WINDOW] + [(now, want)]
            p.scale_to(want if want >= cur else min(cur, max(w for _, w in wants)), now)  # scale-down stabilisation


class S3NoBudget(S3):
    """Ablation: every violation priced at the full penalty, no error-budget feedback; static quota reserve."""
    name = "s3_no_budget"
    BUDGET = False


class S3NoTrend(S3):
    """Ablation: planner keeps its demand signal and 80% target but does not extrapolate the trend."""
    name = "s3_no_trend"
    TREND = False


class S3Reactive(S3):
    """Ablation: S3 router, but the pool is scaled by the reactive HPA (70% target on its own utilisation)
    instead of the demand-forecast planner."""
    name = "s3_reactive"
    PROACTIVE = False


BASELINES = (RoundRobin, Cheapest, LatencyOnly, PerProviderAutoscale, TieredStatic)
ABLATIONS = (S3NoBudget, S3NoTrend, S3Reactive)
POLICIES = {p.name: p for p in (*BASELINES, S3, *ABLATIONS)}
