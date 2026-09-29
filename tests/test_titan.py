from dataclasses import replace

import pytest

from titan_fleet import oracle
from titan_fleet.bench import API_A_EAST, FLEET, POOL, SCENARIOS
from titan_fleet.policies import POLICIES, S3, S3NoBudget, TieredStatic
from titan_fleet.sim import Request, Scenario, Sim, run, workload

SHORT = Scenario("short_burst", "15 min with a x3 burst", FLEET, duration=900.0,
                 bursts=((300, 480, 3, 3, None),))


def test_workload_is_seeded_and_shaped():
    a, b = workload(SHORT, 1), workload(SHORT, 1)
    assert [(r.t, r.tin, r.z1) for r in a] == [(r.t, r.tin, r.z1) for r in b]
    assert [r.t for r in workload(SHORT, 2)] != [r.t for r in a]
    in_burst = sum(300 <= r.t < 480 for r in a) / 180
    before = sum(r.t < 300 for r in a) / 300
    assert in_burst > 2 * before


def test_managed_quota_throttles_then_refills():
    scn = Scenario("q", "", (replace(API_A_EAST, tpm=6000),))
    sim = Sim(scn)
    r = Request(0.0, "interactive", 2000, 1000, 0.0, 0.0)
    assert sim.attempt("apiA-east", r, 0.0)[0] == "ok"
    assert sim.attempt("apiA-east", r, 0.0)[0] == "ok"
    assert sim.attempt("apiA-east", r, 0.0)[0] == "429"
    assert sim.attempt("apiA-east", r, 30.0)[0] == "ok"  # half a minute refills 3000 tokens


def test_pool_cold_start_queueing_and_billing():
    scn = Scenario("p", "", (replace(POOL, slots=1, init_replicas=1, min_replicas=1),))
    sim = Sim(scn)
    p = sim.pools["selfhost-east"]
    r = Request(0.0, "batch", 100, 100, 0.0, 0.0)
    service = 0.30 + 100 * 0.0001 + 100 * 0.025
    assert sim.attempt("selfhost-east", r, 0.0)[1][0] == pytest.approx(0.31)
    assert p.wait(0.0) == pytest.approx(service)  # the only slot is busy: FIFO wait
    p.scale_to(2, 0.0)  # the new replica is not ready before its cold start
    assert p.wait(0.0) == pytest.approx(service)
    p.scale_to(1, 10.0)  # cancelling the pending replica bills it only until now
    assert p.replica_seconds(100.0) == pytest.approx(100.0 + 10.0)


def test_region_outage_fails_every_endpoint_in_region():
    scn = Scenario("o", "", FLEET, events=((0, 60, "outage", "east", 0),))
    sim = Sim(scn)
    r = Request(1.0, "interactive", 100, 10, 0.0, 0.0)
    assert [sim.attempt(n, r, 1.0)[0] for n in sim.eps] == ["5xx", "5xx", "ok", "5xx"]


def test_violation_price_follows_the_error_budget():
    pol = S3(Sim(SHORT))
    pol.n["interactive"] = 1000
    safe = pol.violation_price("interactive", 300.0)
    pol.viol["interactive"] = 30  # 3% missed against a 1% budget
    assert safe < 0.01 < pol.violation_price("interactive", 300.0) == pytest.approx(0.5, abs=0.01)
    assert S3NoBudget(Sim(SHORT)).violation_price("interactive", 300.0) == 0.5


def test_oracle_is_below_every_policy():
    reqs = workload(SHORT, 3)
    j = oracle.solve(SHORT, reqs)["objective"]
    for p in POLICIES.values():
        assert run(SHORT, reqs, p)["objective"] >= j


def test_mechanism_beats_the_strongest_baseline_on_a_burst():
    """Fails if the S3 router stops seeing queue pressure: a variant blind to pool queueing must lose."""
    class QueueBlind(S3):
        def _late(self, n, r, now):
            return 0.0 if self.sim.eps[n].kind == "pool" else super()._late(n, r, now)

    reqs = workload(SHORT, 5)
    s3 = run(SHORT, reqs, S3)["objective"]
    assert s3 < run(SHORT, reqs, TieredStatic)["objective"]
    assert s3 < run(SHORT, reqs, QueueBlind)["objective"]


def test_at_least_twenty_distinct_scenarios():
    assert len({s.name for s in SCENARIOS}) == len(SCENARIOS) >= 20
