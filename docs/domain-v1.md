# MealCircuit Domain v1

Domain v1 is the language-neutral boundary shared by Python/SQLite, Kotlin/Room, Portable Data and synchronization. The normative machine-readable contract is [`protocol/domain-v1.schema.json`](../protocol/domain-v1.schema.json); fixtures under `protocol/fixtures/` are executed by both Python and Android tests.

## Revision envelope

Every mutable domain object advances through immutable revisions. A revision contains `schema_version`, a stable prefixed `entity_id`, a unique `revision_id`, zero or more parent revision IDs, UTC RFC 3339 creation time, author device ID, tombstone flag and a JSON payload. A conflict resolution revision cites both sibling parents.

New IDs use a domain prefix plus a complete UUIDv4. Legacy 12-hex IDs remain readable and are never bulk-rewritten. Calendar dates such as `record_date` remain literal `YYYY-MM-DD` values; instants are UTC. “Today” is calculated with the user's IANA timezone setting.

Unknown future schema versions may be retained as encrypted opaque entities, but an older client must not materialize, edit or re-encrypt them. Key rotation is blocked until the client is upgraded and all unknown entities are understood.

## Entity families

- `task`, `task_input`, `analysis_result`, `correction`
- `food_item`, including revision history and package-photo asset references
- `daily_record`, `checkin_day`, `checkin_draft`, `daily_review`
- `memory`, `adjustment`
- versioned `preferences` for profile, settings, private doctrine and check-in module configuration
- `asset` metadata; bytes are content-addressed and transferred separately

Task input is intentionally separate from task state so edits can evolve without rewriting locked results. Analysis results are derived entities, not authoritative facts.

## AI provenance and staleness

Every generated result records the exact source entity/revision set, settings and doctrine hashes, result schema version, provider, model and generation time. API keys are never domain data. When a source head changes, the previous result remains present but is reported as stale; it is not silently overwritten or deleted.

## Merge rules

Three-way merge uses a common parent revision:

| Situation | Result |
|---|---|
| Different entity IDs or immutable events | Set union by ID |
| Different fields on the same entity | Automatic recursive merge |
| Different values for the same field | Preserve sibling revisions and create a conflict |
| Concurrent delete and edit | Conflict; deletion is never silently selected |
| Two AI results | Preserve both; select an active result explicitly |
| Timestamp disagreement | Never used as a winner rule |

Maps are merged recursively. Lists of objects with stable IDs are merged as keyed collections; unkeyed concurrent list edits conflict. Client clock order does not decide data loss.

## Storage projections

`domain_revisions` and `entity_heads` are authoritative protocol state. Existing Python tables and Android `materialized_records` are rebuildable projections optimized for current UI and Agent workflows. A local write and its `sync_outbox` entry are committed in one SQLite/Room transaction.

Python configuration files remain editable mirrors. File hash changes create a new preference revision; a synced preference revision is written back atomically. Managed files are addressed by asset ID and SHA-256, never by a portable absolute path.
