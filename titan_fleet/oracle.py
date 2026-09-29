"""Hindsight oracle: a fluid linear program that knows every future arrival, latency draw, outage,
quota cut and price change, and chooses per-minute routing fractions and (fractional) pool replicas.

It relaxes the simulator (no queueing inside a slot, no cold start, fractional replicas, requests of
a class/slot cell are divisible), so its objective is an optimistic reference, not an achievable
policy. SBR is measured against it; a policy beating it would expose a modelling gap and is reported.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from .sim import CLASSES, SLOS, Request, Sim, Scenario, managed_latency, pool_service

SLOT = 60.0


def solve(scn: Scenario, requests: list[Request]) -> dict:
    sim = Sim(scn)  # used only for event lookups
    S = int(np.ceil(scn.duration / SLOT))
    eps = list(sim.eps.values())
    mids = [(s + 0.5) * SLOT for s in range(S)]

    def avail(name, s):
        t0 = s * SLOT
        return sum(not sim.down(name, t0 + k) for k in range(0, int(SLOT), 5)) / (SLOT / 5)

    cells = {}  # (class, slot) -> list of requests
    for r in requests:
        cells.setdefault((r.cls, min(S - 1, int(r.t // SLOT))), []).append(r)

    cols, cost, ub = [], [], []  # decision columns: fraction of a cell served by endpoint e in slot s2
    for (c, s), rs in cells.items():
        slo = SLOS[c]
        m = 0 if slo.metric == "ttft" else 1
        horizon = s + 1 + int(slo.target // SLOT) if m else s
        for s2 in range(s, min(S - 1, horizon) + 1):
            delay = max(0.0, (s2 - s - 1) * SLOT)  # optimistic: deferral to the next slot is free
            for e in eps:
                if avail(e.name, s2) == 0:
                    continue
                slow = sim.factor("slow", e.name, mids[s2])
                lat = managed_latency if e.kind == "managed" else pool_service
                ok = sum(lat(e, r, slow)[m] + delay <= slo.target for r in rs) / len(rs)
                if ok == 0:
                    continue
                cols.append((c, s, s2, e))
                cost.append(sum(sim.request_cost(e.name, r, mids[s2]) for r in rs) if e.kind == "managed" else 0.0)
                ub.append(ok)
    nx = len(cols)
    pools = [e for e in eps if e.kind == "pool"]
    r_col = {(e.name, s): nx + i * S + s for i, e in enumerate(pools) for s in range(S)}
    o_col = {c: nx + len(r_col) + k for k, c in enumerate(CLASSES)}
    nvar = nx + len(r_col) + len(CLASSES)
    c_vec = np.zeros(nvar)
    c_vec[:nx] = cost
    for (name, s), j in r_col.items():
        c_vec[j] = sim.eps[name].gpu_hour * SLOT / 3600
    for c, j in o_col.items():
        c_vec[j] = SLOS[c].penalty

    rows, cc, vals, b = [], [], [], []

    def put(i, j, v):
        rows.append(i)
        cc.append(j)
        vals.append(v)

    cell_row = {k: i for i, k in enumerate(cells)}  # sum of a cell's fractions <= 1
    row = len(cells)
    b += [1.0] * len(cells)
    cap_row = {}
    for e in eps:
        for s in range(S):
            cap_row[(e.name, s)] = row
            row += 1
            b.append(e.tpm * sim.factor("quota", e.name, mids[s]) * SLOT / 60 * avail(e.name, s)
                     if e.kind == "managed" else 0.0)
    viol_row = {c: row + k for k, c in enumerate(CLASSES)}
    n_c = {c: sum(len(rs) for (cl, _), rs in cells.items() if cl == c) for c in CLASSES}
    b += [(1 - SLOS[c].objective) * n_c[c] - n_c[c] for c in CLASSES]
    for j, (c, s, s2, e) in enumerate(cols):
        rs = cells[(c, s)]
        put(cell_row[(c, s)], j, 1.0)
        use = sum(r.tin + r.tout for r in rs) if e.kind == "managed" else \
            sum(pool_service(e, r, sim.factor("slow", e.name, mids[s2]))[1] for r in rs)
        put(cap_row[(e.name, s2)], j, use)  # quota tokens or pool slot-seconds
        put(viol_row[c], j, -float(len(rs)))  # overrun_c >= N_c - served_c - budget_c
    for (name, s), j in r_col.items():  # pool capacity: served slot-seconds <= replicas * slots * available time
        e = sim.eps[name]
        put(cap_row[(name, s)], j, -e.slots * SLOT * avail(name, s))
    for c, j in o_col.items():
        put(viol_row[c], j, -1.0)
    A = coo_matrix((vals, (rows, cc)), shape=(row + len(CLASSES), nvar)).tocsr()
    bounds = [(0, u) for u in ub] + [(sim.eps[n].min_replicas, sim.eps[n].max_replicas) for n, _ in r_col] \
        + [(0, None)] * len(CLASSES)
    res = linprog(c_vec, A_ub=A, b_ub=np.array(b), bounds=bounds, method="highs")
    if res.status != 0:
        raise RuntimeError(f"{scn.name}: oracle LP failed: {res.message}")
    x = res.x
    served = float(sum(x[j] * len(cells[(c, s)]) for j, (c, s, _, _) in enumerate(cols)))
    penalty = float(sum(SLOS[c].penalty * x[j] for c, j in o_col.items()))
    return {"objective": round(float(res.fun), 4), "cost": round(float(res.fun) - penalty, 4),
            "penalty": round(penalty, 4), "served_fraction": round(served / max(1, len(requests)), 4),
            "overrun": {c: round(float(x[j]), 1) for c, j in o_col.items()},
            "mean_replicas": {e.name: round(float(np.mean([x[r_col[(e.name, s)]] for s in range(S)])), 2)
                              for e in pools}}
