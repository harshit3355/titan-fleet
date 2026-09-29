"""CLI: list scenarios, simulate one policy on one scenario, or run the full benchmark."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bench as bench_mod
from . import oracle
from .policies import POLICIES
from .sim import run, workload


def prometheus(res: dict, scenario: str, policy: str) -> str:
    """The run's counters in Prometheus text exposition format."""
    lab = f'scenario="{scenario}",policy="{policy}"'
    L = ["# TYPE titan_requests_total counter", "# TYPE titan_slo_violations_total counter",
         "# TYPE titan_endpoint_errors_total counter", "# TYPE titan_cost_usd_total counter"]
    for c, x in res["classes"].items():
        L.append(f'titan_requests_total{{{lab},class="{c}"}} {x["requests"]}')
        L.append(f'titan_slo_violations_total{{{lab},class="{c}"}} {x["violations"]}')
    for e, errs in res["errors"].items():
        L += [f'titan_endpoint_errors_total{{{lab},endpoint="{e}",status="{s}"}} {n}' for s, n in errs.items()]
    for e, v in {**res["cost_managed"], **res["cost_pool"]}.items():
        L.append(f'titan_cost_usd_total{{{lab},endpoint="{e}"}} {v}')
    return "\n".join(L) + "\n"


def _write(base: Path, report: dict, md: str) -> None:
    base.parent.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8", newline="\n")
    base.with_suffix(".md").write_text(md, encoding="utf-8", newline="\n")
    print(f"wrote {base.with_suffix('.json')} and {base.with_suffix('.md')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m titan_fleet", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list scenarios and policies")
    r = sub.add_parser("run", help="simulate one policy on one scenario; print summary and Prometheus metrics")
    r.add_argument("scenario", choices=list(bench_mod.BY_NAME))
    r.add_argument("policy", choices=list(POLICIES))
    r.add_argument("--seed", type=int, default=11)
    b = sub.add_parser("bench", help="all policies x all scenarios x seeds -> reports/benchmark.{json,md}")
    b.add_argument("--seed", type=int, default=11)
    b.add_argument("--seeds", type=int, default=3)
    b.add_argument("--only", nargs="*", help="restrict to these scenarios")
    b.add_argument("--out", default="reports")
    a = ap.parse_args(argv)

    if a.cmd == "list":
        for s in bench_mod.SCENARIOS:
            print(f"{s.name:22s} {s.about}")
        print("\npolicies:", ", ".join(POLICIES))
        return 0
    if a.cmd == "run":
        scn = bench_mod.BY_NAME[a.scenario]
        reqs = workload(scn, a.seed)
        res, orc = run(scn, reqs, POLICIES[a.policy]), oracle.solve(scn, reqs)
        print(json.dumps({"requests": len(reqs), "objective": res["objective"], "cost": res["cost"],
                          "penalty": res["penalty"], "oracle_objective": orc["objective"],
                          "sbr": round(res["objective"] - orc["objective"], 4), "classes": res["classes"]}, indent=1))
        print(prometheus(res, a.scenario, a.policy), end="")
        return 0
    rep = bench_mod.bench(a.seed, a.seeds, a.only)
    rep["provenance"] = bench_mod.provenance(seed=a.seed, seeds=a.seeds)
    for p, x in rep["summary"].items():
        print(f"{p:24s} total SBR {x['total_sbr']:10.2f}  median SBR/J* {x['median_sbr_rel']:.3f}  best in {x['best_in']}")
    print("negative cases:", [x["scenario"] for x in rep["negative_cases"]])
    _write(Path(a.out) / "benchmark", rep, bench_mod.markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
