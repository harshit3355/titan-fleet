# Threat model

TITAN FLEET v0.1 is a simulator and a benchmark. It calls no model endpoint, holds no credential and
changes no infrastructure. This document covers the v0.1 artifact and the boundaries a real
deployment of the controller would have to keep.

## Assets

- Routing and scaling decisions: where each request's prompt goes, and how many GPUs are billed.
- Provider credentials and quotas (in a real deployment; none exist here).
- Spend: managed-API tokens and GPU-hours.
- Data residency: which region and provider sees a prompt.
- The integrity of the benchmark report used as evidence.

## Trust boundaries

| Input | Trust | Why |
|---|---|---|
| Scenario and fleet definitions (`titan_fleet/bench.py`) | trusted, code-reviewed | they define what "good" means; a wrong quota or price makes every result wrong in the same direction |
| SLO classes, objectives and penalties (`titan_fleet/sim.py`) | trusted, code-reviewed | the penalty weights decide the cost-vs-SLO trade the controller optimises |
| Request metadata (class, token counts) | **untrusted** in a real deployment | a client that labels background work "interactive" buys priority and managed quota |
| Provider health signals (errors, latency, 429s) | untrusted, noisy | a flapping or lying endpoint can attract or repel traffic |
| scipy (oracle only) | trusted, version-floored | used for the hindsight LP; not on any decision path of the controller |

## Threats and controls

| Threat | Control in v0.1 | What a real deployment needs |
|---|---|---|
| Class spoofing: tenants mark traffic interactive | not controlled (simulated classes are honest) | derive class from authenticated workload identity, not a request header; quota per tenant |
| Budget exhaustion as denial of service: a flood burns the interactive error budget, so the controller moves everyone to expensive endpoints | penalties are bounded per violation; `violation_price` saturates at the class penalty | per-tenant rate limits upstream of the router; spend caps that fail closed |
| Runaway scaling: forecast error or a hostile burst provisions GPUs | replicas clamped to `[min_replicas, max_replicas]`; scale-down stabilisation | hard spend ceiling enforced outside the controller; scaling proposals reviewed or rate-limited |
| Residency violation: failover sends a prompt to a region or provider it may not reach | not modelled: every endpoint is treated as allowed for every request | residency as a hard filter before scoring, never a cost term |
| Silent failover to a different model | not modelled: all endpoints are assumed to serve an equivalent model | route only within a declared model-equivalence group |
| Retry storms amplify an outage | at most 3 attempts per request; cooldowns after 5xx/429/503 | same, plus a global retry budget |
| A tampered report is presented as evidence | reports record commit SHA, command, seeds, scipy version, scenario hash and UTC time; the benchmark is deterministic, so regenerate from the commit to verify | signed reports |
| Supply-chain compromise of CI actions | all actions pinned by commit SHA; workflow token is read-only | same |

## Out of scope for v0.1

Real provider adapters and credentials, Kubernetes or Container Apps scaling, multi-tenant isolation,
residency and model-equivalence constraints, and signing of reports.
