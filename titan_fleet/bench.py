"""Benchmark: every policy on every scenario and seed, scored by SLO-Budget Regret against the oracle.

objective J = cost + sum_c penalty_c * max(0, violations_c - error_budget_c)
SBR        = J(policy) - J(oracle)
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import scipy

from . import oracle
from .policies import ABLATIONS, BASELINES, POLICIES, S3
from .sim import CLASSES, SLOS, Endpoint, Scenario, run, workload

ROOT = Path(__file__).resolve().parent.parent

# Illustrative parameters only: not vendor-published latencies, quotas or prices.
POOL = Endpoint("selfhost-east", "pool", "east", ttft=0.30, tpot=0.025, sigma=0.1, prefill=0.0001, slots=8,
                gpu_hour=2.50, cold_start=240.0, min_replicas=1, max_replicas=16, init_replicas=6)
API_A_EAST = Endpoint("apiA-east", "managed", "east", ttft=0.50, tpot=0.015, sigma=0.35,
                      price_in=0.0025, price_out=0.010, tpm=120_000)   # Azure-OpenAI-like
API_A_WEST = Endpoint("apiA-west", "managed", "west", ttft=0.60, tpot=0.015, sigma=0.35,
                      price_in=0.0025, price_out=0.010, tpm=80_000)
API_B_EAST = Endpoint("apiB-east", "managed", "east", ttft=0.90, tpot=0.020, sigma=0.45,
                      price_in=0.0012, price_out=0.005, tpm=150_000)   # Bedrock-like: cheaper, slower
FLEET = (POOL, API_A_EAST, API_A_WEST, API_B_EAST)


def _s(name, about, fleet=FLEET, **kw) -> Scenario:
    return Scenario(name, about, fleet, **kw)


def _pool(**kw) -> tuple:
    return (replace(POOL, **kw), API_A_EAST, API_A_WEST, API_B_EAST)


SQUARE = tuple((300 + 600 * k, 600 + 600 * k, 2.5, 2.5, None) for k in range(6))
SCENARIOS = [
    _s("steady", "flat load, no incidents", diurnal=0.0),
    _s("diurnal", "one compressed day, +/-50% sinusoid", diurnal=0.5),
    _s("burst_3x_5min", "all classes x3 for 5 minutes", bursts=((1200, 1500, 3, 3, None),)),
    _s("burst_interactive_4x", "interactive x4 for 5 minutes", bursts=((1200, 1500, 4, 4, ("interactive",)),)),
    _s("batch_flood", "batch x4 for 15 minutes", bursts=((900, 1800, 4, 4, ("batch",)),)),
    _s("flash_crowd_8x_90s", "all classes x8 for 90 seconds", bursts=((1800, 1890, 8, 8, None),)),
    _s("slow_ramp", "linear ramp to x2.5 over 40 minutes, then hold",
       bursts=((0, 2400, 1, 2.5, None), (2400, 3600, 2.5, 2.5, None))),
    _s("square_wave", "load alternates x1 / x2.5 every 5 minutes", diurnal=0.0, bursts=SQUARE),
    _s("outage_apiA_east", "apiA-east returns 5xx for 15 minutes", events=((1200, 2100, "outage", "apiA-east", 0),)),
    _s("outage_region_east", "region east down for 10 minutes (self-hosted pool, apiA-east, apiB-east)",
       events=((1500, 2100, "outage", "east", 0),)),
    _s("outage_region_west", "region west down for 20 minutes", events=((1200, 2400, "outage", "west", 0),)),
    _s("outage_selfhost", "self-hosted pool down for 10 minutes", events=((1200, 1800, "outage", "selfhost-east", 0),)),
    _s("quota_cut_apiA", "apiA-east quota cut to 30% after 20 minutes",
       events=((1200, 3600, "quota", "apiA-east", 0.3),)),
    _s("quota_exhaustion", "all managed quotas at 40% plus a x2 burst",
       events=tuple((0, 3600, "quota", n, 0.4) for n in ("apiA-east", "apiA-west", "apiB-east")),
       bursts=((1500, 2100, 2, 2, None),)),
    _s("price_drop_apiB", "apiB-east price falls to 30% after 20 minutes",
       events=((1200, 3600, "price", "apiB-east", 0.3),)),
    _s("price_spike_apiA", "apiA prices x3 after 20 minutes",
       events=((1200, 3600, "price", "apiA-east", 3), (1200, 3600, "price", "apiA-west", 3))),
    _s("cold_start_storm", "15-minute cold starts during a x3 burst", fleet=_pool(cold_start=900.0),
       bursts=((1200, 2400, 3, 3, None),)),
    _s("gpu_price_high", "GPU-hour price x5", fleet=_pool(gpu_hour=12.5)),
    _s("low_load", "0.8 req/s: idle GPU capacity dominates", rate=0.8),
    _s("brownout_apiA_east", "apiA-east latency x4 (no errors) for 15 minutes",
       events=((1200, 2100, "slow", "apiA-east", 4),)),
    _s("selfhost_degraded", "self-hosted pool latency x2 for 20 minutes",
       events=((1200, 2400, "slow", "selfhost-east", 2),)),
    _s("interactive_only", "100% interactive traffic", mix=(1.0, 0.0, 0.0)),
    _s("background_heavy", "60% background traffic", mix=(0.2, 0.2, 0.6)),
    _s("burst_during_outage", "x2.5 burst while apiA-east is down",
       events=((1200, 2100, "outage", "apiA-east", 0),), bursts=((1500, 1800, 2.5, 2.5, None),)),
    _s("high_load", "5 req/s sustained", rate=5.0),
]
BY_NAME = {s.name: s for s in SCENARIOS}
ORDER = [p.name for p in (*BASELINES, S3, *ABLATIONS)]
BASELINE_NAMES = [p.name for p in BASELINES]


def fixture_hash() -> str:
    blob = json.dumps([dataclasses.asdict(s) for s in SCENARIOS], sort_keys=True, default=str)
    blob += json.dumps({c: dataclasses.asdict(v) for c, v in SLOS.items()}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def provenance(**extra) -> dict:
    def git(*args):
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    sha = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--", "titan_fleet"))
    return {"commit": (sha or "unknown") + ("-dirty" if dirty else ""),
            "command": "python -m titan_fleet " + " ".join(sys.argv[1:]),
            "python": platform.python_version(), "platform": platform.platform(), "scipy": scipy.__version__,
            "scenarios_sha256": fixture_hash(), "workload": "synthetic (seeded non-homogeneous Poisson)",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **extra}


def _mean(xs):
    return round(statistics.fmean(xs), 4)


def bench_scenario(scn: Scenario, seeds: list[int]) -> dict:
    runs = {p: [] for p in ORDER}
    oracles = []
    for seed in seeds:
        reqs = workload(scn, seed)
        orc = oracle.solve(scn, reqs)
        oracles.append(orc)
        for p in ORDER:
            res = run(scn, reqs, POLICIES[p])
            res["sbr"] = round(res["objective"] - orc["objective"], 4)
            res.pop("replica_trace")
            runs[p].append(res)
    J_star = _mean([o["objective"] for o in oracles])
    pol = {}
    for p, rs in runs.items():
        pol[p] = {"sbr": _mean([r["sbr"] for r in rs]), "objective": _mean([r["objective"] for r in rs]),
                  "cost": _mean([r["cost"] for r in rs]), "penalty": _mean([r["penalty"] for r in rs]),
                  "sbr_rel": round(_mean([r["sbr"] for r in rs]) / J_star, 4),
                  "attainment": {c: _mean([r["classes"][c]["attainment"] for r in rs])
                                 for c in CLASSES if rs[0]["classes"][c]["requests"]},
                  "p99_interactive_ttft": _mean([r["classes"]["interactive"]["p99_ttft"] for r in rs])
                  if rs[0]["classes"]["interactive"]["requests"] else None,
                  "http_429": _mean([sum(e["429"] for e in r["errors"].values()) for r in rs]),
                  "http_5xx": _mean([sum(e["5xx"] for e in r["errors"].values()) for r in rs]),
                  "failed_requests": _mean([r["failed_requests"] for r in rs]),
                  "replica_hours": _mean([sum(r["replica_hours"].values()) for r in rs]),
                  "runs": rs}
    return {"scenario": scn.name, "about": scn.about, "requests": [len(workload(scn, s)) for s in seeds],
            "oracle": {"objective": J_star, "cost": _mean([o["cost"] for o in oracles]),
                       "penalty": _mean([o["penalty"] for o in oracles]), "runs": oracles},
            "policies": pol}


def bench(seed: int, n_seeds: int, only: list[str] | None = None) -> dict:
    seeds = [seed + k for k in range(n_seeds)]
    t0 = time.perf_counter()
    rows = [bench_scenario(s, seeds) for s in SCENARIOS if not only or s.name in only]
    best_base = {r["scenario"]: min(BASELINE_NAMES, key=lambda p: r["policies"][p]["sbr"]) for r in rows}
    summary = {}
    for p in ORDER:
        sbr = [r["policies"][p]["sbr"] for r in rows]
        summary[p] = {"total_sbr": round(sum(sbr), 2), "median_sbr_rel": round(statistics.median(
                          r["policies"][p]["sbr_rel"] for r in rows), 4),
                      "total_cost": round(sum(r["policies"][p]["cost"] for r in rows), 2),
                      "total_penalty": round(sum(r["policies"][p]["penalty"] for r in rows), 2),
                      "best_in": sum(min(ORDER, key=lambda q: r["policies"][q]["sbr"]) == p for r in rows)}
    s3_vs = {b: sum(r["policies"]["s3"]["sbr"] < r["policies"][b]["sbr"] for r in rows) for b in ORDER if b != "s3"}
    negatives = [{"scenario": r["scenario"], "about": r["about"], "best_baseline": best_base[r["scenario"]],
                  "s3_sbr": r["policies"]["s3"]["sbr"], "baseline_sbr": r["policies"][best_base[r["scenario"]]]["sbr"]}
                 for r in rows if r["policies"]["s3"]["sbr"] > r["policies"][best_base[r["scenario"]]]["sbr"]]
    below_oracle = [(r["scenario"], p) for r in rows for p in ORDER if r["policies"][p]["sbr"] < 0]
    return {"seeds": seeds, "scenarios": len(rows), "policies": ORDER,
            "slos": {c: dataclasses.asdict(v) for c, v in SLOS.items()},
            "fleet": [dataclasses.asdict(e) for e in FLEET],
            "summary": summary, "s3_better_than": s3_vs, "negative_cases": negatives,
            "policies_below_oracle": below_oracle, "wall_seconds": round(time.perf_counter() - t0, 1),
            "per_scenario": rows}


LABELS = {"round_robin": "Round robin + HPA", "cheapest": "Cheapest endpoint + HPA",
          "latency_only": "Latency-only + HPA", "per_provider_autoscale": "Per-provider autoscaling (static failover + HPA)",
          "tiered_static": "Tiered static routing + HPA (extra baseline)",
          "s3": "**S3 controller (mechanism)**", "s3_no_budget": "S3 without error-budget term (ablation)",
          "s3_no_trend": "S3 without trend look-ahead (ablation)",
          "s3_reactive": "S3 without proactive scaling (ablation)"}


def markdown(rep: dict) -> str:
    s, n = rep["summary"], rep["scenarios"]
    L = ["# TITAN FLEET benchmark: SLO-Budget Regret", "",
         "_Simulated fleet, synthetic workload, illustrative parameters. Every number below was produced by the "
         "command in the provenance section._", "",
         f"- {n} scenarios x {len(rep['seeds'])} seeds (seeds {rep['seeds']}), one simulated hour each, "
         "~3 requests/s across three SLO classes.",
         "- Objective J = cost + sum over classes of penalty x violations beyond the error budget. "
         "SBR = J(policy) - J(oracle). The oracle is a fluid LP with full hindsight "
         "(see `titan_fleet/oracle.py`); it is optimistic, not achievable.",
         f"- Policies with negative SBR anywhere (would indicate an oracle modelling gap): "
         f"{rep['policies_below_oracle'] or 'none'}.", "",
         "| SLO class | Metric | Target | Objective | Penalty per violation over budget |", "|---|---|---|---|---|"]
    L += [f"| {c} | {v['metric']} | {v['target']} s | {100 * v['objective']:.0f}% | ${v['penalty']} |"
          for c, v in rep["slos"].items()]
    L += ["", "## Summary over all scenarios", "",
          "| Policy | Total SBR ($) | Median SBR / J* | Total cost ($) | Total penalty ($) | Best in |",
          "|---|---|---|---|---|---|"]
    for p in rep["policies"]:
        x = s[p]
        L.append(f"| {LABELS[p]} | {x['total_sbr']} | {100 * x['median_sbr_rel']:.1f}% | {x['total_cost']} | "
                 f"{x['total_penalty']} | {x['best_in']}/{n} |")
    L += ["", "S3 has lower SBR than: " + ", ".join(f"{p} in {k}/{n}" for p, k in rep["s3_better_than"].items()) + ".",
          "", "## Negative cases: S3 worse than the best baseline", ""]
    if rep["negative_cases"]:
        L += ["| Scenario | What happens | Best baseline | Baseline SBR | S3 SBR |", "|---|---|---|---|---|"]
        L += [f"| {x['scenario']} | {x['about']} | {x['best_baseline']} | {x['baseline_sbr']} | {x['s3_sbr']} |"
              for x in rep["negative_cases"]]
    else:
        L.append("None in this run.")
    L += ["", "## SBR per scenario ($, mean over seeds)", "",
          "| Scenario | J* (oracle) | " + " | ".join(rep["policies"]) + " |",
          "|---|---|" + "---|" * len(rep["policies"])]
    for r in rep["per_scenario"]:
        best = min(rep["policies"], key=lambda p: r["policies"][p]["sbr"])
        L.append(f"| {r['scenario']} | {r['oracle']['objective']} | " + " | ".join(
            (f"**{r['policies'][p]['sbr']}**" if p == best else str(r["policies"][p]["sbr"])) for p in rep["policies"]) + " |")
    L += ["", "## SLO attainment and cost per scenario (S3 vs the best baseline)", "",
          "| Scenario | Policy | Cost ($) | Penalty ($) | Interactive | Batch | Background | p99 TTFT (s) | 429s | 5xx | Replica-h |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["per_scenario"]:
        bb = min(BASELINE_NAMES, key=lambda p: r["policies"][p]["sbr"])
        for p in ("s3", bb):
            x = r["policies"][p]
            a = x["attainment"]
            L.append(f"| {r['scenario']} | {p} | {x['cost']} | {x['penalty']} | " + " | ".join(
                f"{100 * a[c]:.2f}%" if c in a else "-" for c in CLASSES) +
                f" | {x['p99_interactive_ttft']} | {x['http_429']} | {x['http_5xx']} | {x['replica_hours']} |")
    L += ["", "## Provenance", "", *(f"- {k}: `{v}`" for k, v in rep["provenance"].items()), ""]
    return "\n".join(L)
