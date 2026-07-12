# Adaptive closed-loop verification matrix

This file tracks the approved end-to-end delivery against concrete code and verification evidence. A row is complete only when both implementation and the named verification exist.

| Requirement | Current classification | Code / evidence | Completion gate |
| --- | --- | --- | --- |
| Versioned profile, goals, strategy, metrics | implemented | `personalization.py`; versioned target provenance and optimistic onboarding tests | automated tests pass |
| Per-meal preparation strategy | implemented | onboarding persists breakfast/lunch/dinner modes in the versioned personal strategy; each home-cooked slot has an independent card, constraints, rotation and history | dual lunch/dinner integration and Web tests pass |
| Safety-aware onboarding and eligibility | implemented | one `generation_policy()` / `require_generation()` gate covers Web, CLI, context, generate, complete and rescue | standard, setup, clinician-guided and restricted tests pass |
| Restricted-mode schemas and settings | implemented | fact-only Result v2 schemas; restricted settings and plan lookup suppress old targets/prescriptions | no-leak and old-plan tests pass |
| Evidence capture and review requeue | implemented | task evidence links, capture UI/CLI, correction requeue and manifest IDs | integration tests pass |
| Plan execution feedback | implemented | materialized current state plus append-only revision events and optimistic version | revision/history tests pass |
| Candidate learning and confirmed rules | implemented | deterministic thresholds; user decision; goal/profile/strategy/safety/policy scope | constraint and counterexample tests pass |
| Constrained planning and rescue | implemented | immutable plan projections, Result v2 hard-constraint compiler, scoped rescue provenance | invalid-plan and rescue feedback tests pass |
| Inventory and carry-over | implemented | inventory events in context, planning, rescue, Web/CLI and portable bundle | round-trip tests pass |
| Context / Result v2 and agent-run audit | implemented | doctrine hash; profile/goal/strategy/target; versioned rule/experiment; policy/schema/validator/run IDs | generated and external-agent run tests pass |
| Web closed-loop journey | verified | `/setup`, `/`, `/capture`, `/plans`, `/questions`, `/learning`, `/inventory`, `/profile`, `/insights`, `/data`, `/rescue` | HTTP integration and isolated real-browser flow passed, including experiment lifecycle, metric history, completed rescue and feedback write-back |
| CLI closed-loop journey | implemented | setup/plan/feedback/questions/learning/inventory/evidence/rescue/metric/calibration/export/import | CLI integration passes |
| Data portability and recovery | implemented | SHA manifest, integrity/schema preview, pre-restore backup, atomic DB replacement, Web/CLI | round trip passes |
| Backward compatibility | implemented | prefill active/legacy settings; setup only gates generation; legacy overview remains reachable | legacy suite passes |
| Accessibility | verified | semantic forms/errors, keyboard focus trap/inert drawer, responsive grids, reduced motion | automated markup and real-browser 320/720/1440 effective-viewport checks pass with one h1, zero unlabeled controls, no horizontal overflow and no console warnings/errors |
| Full verification and draft PR | local verification complete | 69/69 tests on Python 3.12.13 and 3.13; isolated real-browser dual-meal flow passed at 320/1440 px; [Draft PR #14](https://github.com/QianQIUlp/meal-circuit/pull/14) remains open against `main` | GitHub push and pull-request checks pass |

## Classification decision

- **Keep:** migrations-before-write backup, versioned personalization foundation, evidence links, deterministic candidate thresholds, inventory events, review provenance, optimistic concurrency, and current passing tests.
- **Fix:** safety authorization, target provenance, restricted schemas/settings, append-only feedback history, learning scope, migration registry, source manifest, agent runs, rescue validation, legacy transition, and UI accessibility gaps.
- **Remove:** no complete module is removed. Prescriptive fields in restricted schemas, unconfirmed candidate effects entering the hard planning context, and any path that bypasses authorization are removed or replaced in place.
