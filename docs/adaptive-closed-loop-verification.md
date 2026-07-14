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
| AgentContextV2 selection and inspector | implemented | five context layers, selected/excluded reasons, hashes, knowledge and user-model versions; human Web inspector plus JSON export | selection and Web route tests pass |
| Three-stage case planning | implemented | CaseFormulationV1 → DailyPlanV3 → PlanReviewV1; ≤3 decision-changing questions, per-stage retries, one reviewed revision | provider-boundary, clarification, approval and failure tests pass |
| Negotiable draft lifecycle | implemented | persistent states, 30-second debounce, automatic stale triggers, context CAS, local meal revision and explicit accept | stale/concurrency/local-revision/history tests pass |
| Executable portion contract | implemented | gram range, measurement basis, household measure, nutrition range/confidence and increase/decrease conditions per meal | portion and target-overlap assertions pass |
| Evidence-backed user model | implemented | versioned claims/evidence/counterevidence/rollback; one explicit or two independent real signals; model hypotheses excluded from activation | evidence, high-risk and Portable projection tests pass |
| Offline professional basis | implemented | versioned applicable WHO/NIDDK/ACOG/DGA/sports-nutrition principles, source metadata and boundaries; no runtime network | knowledge-selection and restricted-safety tests pass |
| Web closed-loop journey | implemented and browser-checked | `/` Agent workspace, `/agent/context`, `/learning`, `/plans` plus existing capture/inventory/profile/data/rescue flows; isolated Edge flow covered draft → context → accept → satiety feedback → user model | HTTP integration and real browser flow pass |
| CLI closed-loop journey | implemented | agent-intake/context/draft/state/answer/revise/accept, user-model/reflection plus legacy closed-loop and Portable commands | CLI integration passes |
| Android result compatibility | implemented, local build not run | optional Agent summary/rationale/portion fields render from synced `daily_review`; unknown preference kinds remain materialized | local machine has no Android SDK; user requested no further validation or CI wait |
| Data portability and recovery | implemented | SHA manifest, integrity/schema preview, pre-restore backup, atomic DB replacement, Web/CLI | round trip passes |
| Backward compatibility | implemented | prefill active/legacy settings; setup only gates generation; legacy overview remains reachable | legacy suite passes |
| Accessibility | implemented and browser-checked | semantic forms/errors, keyboard focus trap/inert drawer, responsive Agent grids, reduced motion and live status | 1440px/390px: one h1, labeled controls, no overflow, zero console errors; keyboard drawer flow passes |
| Verification snapshot | completed to user-selected boundary | final 144-test run passed with 26 environment skips; release/dependency/protocol/workflow checks and real browser journey passed; Android local build stopped before tasks because SDK is absent | no further full or CI validation per latest user instruction |

## Classification decision

- **Keep:** migrations-before-write backup, versioned personalization foundation, evidence links, deterministic candidate thresholds, inventory events, review provenance, optimistic concurrency, and current passing tests.
- **Fix:** safety authorization, target provenance, restricted schemas/settings, append-only feedback history, learning scope, migration registry, source manifest, agent runs, rescue validation, legacy transition, and UI accessibility gaps.
- **Remove:** no complete module is removed. Prescriptive fields in restricted schemas, unconfirmed candidate effects entering the hard planning context, and any path that bypasses authorization are removed or replaced in place.
