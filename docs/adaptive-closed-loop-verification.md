# Adaptive closed-loop verification matrix

This file tracks the approved end-to-end delivery against concrete code and verification evidence. A row is complete only when both implementation and the named verification exist.

| Requirement | Current classification | Code / evidence | Completion gate |
| --- | --- | --- | --- |
| Versioned profile, goals, strategy, metrics | keep + fix provenance | `mealcircuit/personalization.py`, `profile_versions`, `goal_versions`, `strategy_versions` | target provenance, edit/version flows, and safety-scope tests pass |
| Safety-aware onboarding and eligibility | fix (P0) | `generation_policy()` exists but is not yet enforced everywhere | Web, CLI, context, generate and complete paths share one authorization matrix |
| Restricted-mode schemas and settings | fix (P0) | current observation mode can retain legacy protein target and prescriptive fields | fact-only schemas contain no advice fields; restricted settings expose no legacy target |
| Evidence capture and review requeue | keep | `task_evidence_links`, context integration, correction requeue tests | Web/CLI capture flow and source-manifest coverage pass |
| Plan execution feedback | fix (P0) | current materialized row uses optimistic versioning | append-only feedback revisions preserve every prior state and missing remains unknown |
| Candidate learning and confirmed rules | keep + fix scope | deterministic thresholds and evidence links exist | candidates/rules/experiments bind profile, goal, strategy and safety versions |
| Constrained planning and rescue | fix | plan IDs and rescue sessions exist; hard-constraint compiler/result schema are incomplete | invalid plans cannot commit; rescue is validated, scoped and feeds execution history |
| Inventory and carry-over | keep + integrate | inventory event model and existing carry-over protocol | today plan, rescue, Web/CLI, export and restore use the same inventory state |
| Context / Result v2 and agent-run audit | fix (P0/P1) | context v2/hash exists; `agent_runs` is not wired | full manifest includes doctrine/policy/schema/validator/run provenance and failures are atomic |
| Web closed-loop journey | missing | existing server-rendered UI only exposes legacy surfaces | setup → today → feedback → learning → profile journey passes real-browser QA |
| CLI closed-loop journey | missing | legacy agent CLI only | setup/plan/feedback/learning/inventory/rescue/export/import commands pass integration tests |
| Data portability and recovery | missing | migration backup exists | preview/apply export-import and backup-restore round trips preserve hashes and history |
| Backward compatibility | fix (P0) | legacy settings/profile prefill exists | legacy users retain recording access and receive a resumable review gate without data loss |
| Accessibility | keep + fix | semantic SSR baseline and responsive styles exist | keyboard, focus, error summary, trend alternative, 320–1440 px and 200% zoom checks pass |
| Full verification and draft PR | missing | baseline: 54 tests pass on 2026-07-11 | full automated suite, release check, browser flows, atomic commits, pushed branch and draft PR |

## Classification decision

- **Keep:** migrations-before-write backup, versioned personalization foundation, evidence links, deterministic candidate thresholds, inventory events, review provenance, optimistic concurrency, and current passing tests.
- **Fix:** safety authorization, target provenance, restricted schemas/settings, append-only feedback history, learning scope, migration registry, source manifest, agent runs, rescue validation, legacy transition, and UI accessibility gaps.
- **Remove:** no complete module is removed. Prescriptive fields in restricted schemas, unconfirmed candidate effects entering the hard planning context, and any path that bypasses authorization are removed or replaced in place.
