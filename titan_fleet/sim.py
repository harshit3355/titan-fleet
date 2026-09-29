"""Deterministic, seeded discrete-event simulator of a heterogeneous LLM endpoint fleet.

Managed endpoints (Azure-OpenAI-like, Bedrock-like) have a tokens-per-minute quota (token bucket,
429 when empty), a lognormal TTFT and per-token latency, and a price per 1K tokens. Self-hosted
pools have replicas with a fixed number of concurrent slots, a FIFO queue, a cold start and a
per-GPU-hour price. Every parameter is illustrative, not a vendor-published figure.
"""
from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass

CLASSES = ("interactive", "batch", "background")
TICK = 15.0             # control-loop period (s), identical for every policy
MAX_ATTEMPTS = 3        # first choice plus two fallbacks
MAX_QUEUE_WAIT = 120.0  # self-hosted admission control: 503 if the queueing delay would exceed this
# status -> (seconds lost before the router can retry, cooldown applied to that endpoint)
PENALTY = {"5xx": (1.0, 30.0), "429": (0.1, 5.0), "503": (0.05, 5.0)}


@dataclass(frozen=True)
class SLO:
    metric: str       # "ttft" or "e2e"
    target: float     # seconds
    objective: float  # fraction of requests that must meet the target; 1 - objective is the error budget
    penalty: float    # $ per violation beyond the error budget (the SBR weight)
    tokens_in: int    # median prompt tokens of this class
    tokens_out: int   # median output tokens


SLOS = {"interactive": SLO("ttft", 1.2, 0.99, 0.50, 600, 200),
        "batch": SLO("e2e", 60.0, 0.95, 0.10, 1500, 400),
        "background": SLO("e2e", 600.0, 0.90, 0.02, 3000, 600)}


@dataclass(frozen=True)
class Endpoint:
    name: str
    kind: str               # "managed" | "pool"
    region: str
    ttft: float             # median TTFT (managed) or base TTFT excluding prefill (pool), s
    tpot: float             # median seconds per output token
    sigma: float            # lognormal spread of latency
    price_in: float = 0.0   # $ / 1K input tokens (managed)
    price_out: float = 0.0  # $ / 1K output tokens (managed)
    tpm: int = 0            # tokens-per-minute quota (managed)
    prefill: float = 0.0    # s per input token (pool)
    slots: int = 0          # concurrent requests per replica (pool)
    gpu_hour: float = 0.0   # $ per replica-hour, billed from provisioning start (pool)
    cold_start: float = 0.0
    min_replicas: int = 0
    max_replicas: int = 0
    init_replicas: int = 0


@dataclass(frozen=True)
class Scenario:
    name: str
    about: str
    endpoints: tuple
    duration: float = 3600.0
    rate: float = 3.0                # mean arrivals/s before shaping
    mix: tuple = (0.5, 0.3, 0.2)     # share of interactive / batch / background
    diurnal: float = 0.3             # amplitude of one sinusoidal period over the run (a compressed day)
    bursts: tuple = ()               # (t0, t1, mult_at_t0, mult_at_t1, classes | None); linear in between
    events: tuple = ()               # (t0, t1, kind, target, value); kind in outage|quota|price|slow

    def rate_at(self, t: float, c: int) -> float:
        r = self.rate * self.mix[c] * (1 - self.diurnal * math.cos(2 * math.pi * t / self.duration))
        for t0, t1, m0, m1, classes in self.bursts:
            if t0 <= t < t1 and (classes is None or CLASSES[c] in classes):
                r *= m0 + (m1 - m0) * (t - t0) / (t1 - t0)
        return r


@dataclass(slots=True)
class Request:
    t: float
    cls: str
    tin: int
    tout: int
    z1: float  # latency noise, drawn once so every policy sees the same request (common random numbers)
    z2: float


def workload(scn: Scenario, seed: int) -> list[Request]:
    """Non-homogeneous Poisson arrivals per class by thinning. Synthetic: no public trace is used."""
    rng = random.Random(f"{scn.name}:{seed}")
    out = []
    for c, cls in enumerate(CLASSES):
        if not scn.mix[c]:
            continue
        peak = scn.rate * scn.mix[c] * (1 + scn.diurnal)
        for _, _, m0, m1, classes in scn.bursts:
            if classes is None or cls in classes:
                peak *= max(m0, m1, 1.0)
        slo, t = SLOS[cls], 0.0
        while True:
            t += rng.expovariate(peak)
            if t >= scn.duration:
                break
            if rng.random() * peak < scn.rate_at(t, c):
                out.append(Request(t, cls, max(16, int(slo.tokens_in * math.exp(0.5 * rng.gauss(0, 1)))),
                                   max(8, int(slo.tokens_out * math.exp(0.5 * rng.gauss(0, 1)))),
                                   rng.gauss(0, 1), rng.gauss(0, 1)))
    out.sort(key=lambda r: r.t)
    return out


def pool_service(e: Endpoint, r: Request, slow: float = 1.0, noise: bool = True) -> tuple[float, float]:
    """(time to first token, total service time) on a free pool slot."""
    a, b = (math.exp(0.1 * r.z1), math.exp(0.1 * r.z2)) if noise else (1.0, 1.0)
    ttft = (e.ttft + r.tin * e.prefill) * slow * a
    return ttft, ttft + r.tout * e.tpot * slow * b


def managed_latency(e: Endpoint, r: Request, slow: float = 1.0) -> tuple[float, float]:
    ttft = e.ttft * slow * math.exp(e.sigma * r.z1)
    return ttft, ttft + r.tout * e.tpot * slow * math.exp(0.5 * e.sigma * r.z2)


class Managed:
    def __init__(self, e: Endpoint):
        self.tokens, self.last = float(e.tpm), 0.0

    def refill(self, now: float, quota: float) -> float:
        if now > self.last:
            self.tokens, self.last = min(quota, self.tokens + (now - self.last) * quota / 60.0), now
        self.tokens = min(self.tokens, quota)
        return self.tokens


class Pool:
    """Replicas x slots as one heap of slot-free times; FIFO because requests take the earliest slot."""

    def __init__(self, e: Endpoint):
        self.e, self.replicas, self.free, self.inflight, self.next_id = e, {}, [], [], 0
        self.scale_to(e.init_replicas, 0.0, cold=False)

    def active(self) -> list[int]:
        return [r for r, v in self.replicas.items() if v[2] is None]

    def size(self) -> int:
        return len(self.active())

    def scale_to(self, n: int, now: float, cold: bool = True) -> None:
        act = self.active()
        n = max(self.e.min_replicas, min(self.e.max_replicas, n))
        for _ in range(n - len(act)):
            ready = now + (self.e.cold_start if cold else 0.0)
            rid, self.next_id = self.next_id, self.next_id + 1
            self.replicas[rid] = [now, ready, None, now]  # billed_from, ready_at, removed_at, busy_until
            for _ in range(self.e.slots):
                heapq.heappush(self.free, (ready, rid))
        if n < len(act):  # cancel pending cold starts first, then drain the least-busy replicas
            victims = sorted(act, key=lambda r: (self.replicas[r][1] <= now, self.replicas[r][3]))
            for r in victims[:len(act) - n]:
                self.replicas[r][2] = max(now, self.replicas[r][3])

    def _top(self) -> float | None:
        while self.free and self.replicas[self.free[0][1]][2] is not None:
            heapq.heappop(self.free)
        return self.free[0][0] if self.free else None

    def wait(self, now: float) -> float:
        f = self._top()
        return math.inf if f is None else max(0.0, f - now)

    def admit(self, now: float, service: float) -> float:
        start = max(now, self._top())
        _, rid = heapq.heappop(self.free)
        heapq.heappush(self.free, (start + service, rid))
        self.replicas[rid][3] = max(self.replicas[rid][3], start + service)
        heapq.heappush(self.inflight, start + service)
        return start - now

    def outstanding(self, now: float) -> int:
        """Admitted requests not yet finished (running + queued)."""
        while self.inflight and self.inflight[0] <= now:
            heapq.heappop(self.inflight)
        return len(self.inflight)

    def replica_seconds(self, end: float) -> float:
        return sum((v[2] if v[2] is not None else max(end, v[3])) - v[0] for v in self.replicas.values())


class Sim:
    def __init__(self, scn: Scenario):
        self.scn = scn
        self.eps = {e.name: e for e in scn.endpoints}
        self.managed = {e.name: Managed(e) for e in scn.endpoints if e.kind == "managed"}
        self.pools = {e.name: Pool(e) for e in scn.endpoints if e.kind == "pool"}
        self.cool = {n: -1.0 for n in self.eps}
        self.cost_managed = {n: 0.0 for n in self.managed}
        self.errors = {n: {s: 0 for s in PENALTY} for n in self.eps}
        self.served = {n: {c: 0 for c in CLASSES} for n in self.eps}
        self.lat = {c: [] for c in CLASSES}
        self.n = {c: 0 for c in CLASSES}
        self.viol = {c: 0 for c in CLASSES}
        self.failed = 0
        self.replica_trace = []

    def factor(self, kind: str, name: str, t: float) -> float:
        e, f = self.eps[name], 1.0
        for t0, t1, k, target, v in self.scn.events:
            if k == kind and t0 <= t < t1 and target in (name, e.region):
                f *= v
        return f

    def down(self, name: str, t: float) -> bool:
        e = self.eps[name]
        return any(k == "outage" and t0 <= t < t1 and target in (name, e.region)
                   for t0, t1, k, target, _ in self.scn.events)

    def quota(self, name: str, t: float) -> float:
        return self.eps[name].tpm * self.factor("quota", name, t)

    def headroom(self, name: str, t: float) -> float:
        return self.managed[name].refill(t, self.quota(name, t))

    def request_cost(self, name: str, r: Request, t: float) -> float:
        e, m = self.eps[name], self.factor("price", name, t)
        return (r.tin * e.price_in + r.tout * e.price_out) * m / 1000.0

    def attempt(self, name: str, r: Request, t: float):
        """Returns (status, payload): ("ok", (ttft, e2e) of this attempt) or (error status, None)."""
        if self.down(name, t):
            return "5xx", None
        e, slow = self.eps[name], self.factor("slow", name, t)
        if e.kind == "managed":
            if self.headroom(name, t) < r.tin + r.tout:
                return "429", None
            self.managed[name].tokens -= r.tin + r.tout
            self.cost_managed[name] += self.request_cost(name, r, t)
            return "ok", managed_latency(e, r, slow)
        p = self.pools[name]
        if p.wait(t) > MAX_QUEUE_WAIT:
            return "503", None
        ttft, total = pool_service(e, r, slow)
        w = p.admit(t, total)
        return "ok", (w + ttft, w + total)

    def record(self, r: Request, outcome) -> None:
        slo = SLOS[r.cls]
        self.n[r.cls] += 1
        if outcome is None:
            self.failed += 1
            self.viol[r.cls] += 1
            self.lat[r.cls].append(math.inf)
            return
        name, ttft, e2e = outcome[:3]
        self.served[name][r.cls] += 1
        x = ttft if slo.metric == "ttft" else e2e
        self.lat[r.cls].append(x)
        self.viol[r.cls] += x > slo.target

    def result(self) -> dict:
        end = self.scn.duration
        cost_pool = {n: p.replica_seconds(end) * p.e.gpu_hour / 3600 for n, p in self.pools.items()}
        classes = {}
        for c in CLASSES:
            slo, n, v = SLOS[c], self.n[c], self.viol[c]
            budget = (1 - slo.objective) * n
            lat = sorted(self.lat[c])
            q = (lambda p: round(lat[min(len(lat) - 1, int(p * len(lat)))], 3) if lat else None)
            classes[c] = {"requests": n, "violations": v, "attainment": round(1 - v / n, 4) if n else None,
                          "error_budget": round(budget, 1), "overrun": round(max(0.0, v - budget), 1),
                          f"p95_{slo.metric}": q(0.95), f"p99_{slo.metric}": q(0.99)}
        cost = sum(self.cost_managed.values()) + sum(cost_pool.values())
        penalty = sum(SLOS[c].penalty * classes[c]["overrun"] for c in CLASSES)
        return {"objective": round(cost + penalty, 4), "cost": round(cost, 4), "penalty": round(penalty, 4),
                "cost_managed": {k: round(v, 4) for k, v in self.cost_managed.items()},
                "cost_pool": {k: round(v, 4) for k, v in cost_pool.items()},
                "replica_hours": {n: round(p.replica_seconds(end) / 3600, 3) for n, p in self.pools.items()},
                "classes": classes, "failed_requests": self.failed, "errors": self.errors, "served": self.served,
                "replica_trace": self.replica_trace}


def run(scn: Scenario, requests: list[Request], policy_cls) -> dict:
    sim = Sim(scn)
    pol = policy_cls(sim)
    next_tick = 0.0

    def tick(now):
        pol.tick(now)
        sim.replica_trace.append([now, *(p.size() for p in sim.pools.values())])

    for r in requests:
        while next_tick <= r.t:
            tick(next_tick)
            next_tick += TICK
        t, outcome = r.t, None
        for name in pol.route(r, t)[:MAX_ATTEMPTS]:
            status, lat = sim.attempt(name, r, t)
            if status == "ok":
                outcome = (name, t - r.t + lat[0], t - r.t + lat[1], lat[0])
                break
            lost, cool = PENALTY[status]
            sim.errors[name][status] += 1
            t += lost
            sim.cool[name] = t + cool
        sim.record(r, outcome)
        pol.observe(r, outcome)
    while next_tick < scn.duration:
        tick(next_tick)
        next_tick += TICK
    return sim.result()
