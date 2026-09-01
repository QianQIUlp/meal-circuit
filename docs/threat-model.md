# Threat model

This document describes the implemented security boundaries for the current MealCircuit release. The design has not received an independent third-party cryptographic audit.

## Protected assets

- meal records, status answers, photos, profile and private doctrine
- Domain revision history and conflict siblings
- Account Data Key, recovery string, API keys and authentication tokens
- integrity and availability of backups and synced state

## Trust boundaries

The local operating system and unlocked user session are trusted. Desktop sync recovery and authentication secrets use the OS keyring; if no secure backend exists, they are session-only and the recovery string is required again. Model-provider API keys remain only in the current Python process and are never persisted to that keyring. Android uses Android Keystore to wrap sync secrets; only wrapped ciphertext is stored in a backup-excluded private preference file. Local SQLite/Room and asset files rely on OS disk/application sandbox encryption and are not SQLCipher-encrypted.

The sync host, database, blob volume, backups and reverse proxy are treated as honest-but-curious for confidentiality. TLS is still mandatory outside explicit localhost debug because E2EE does not hide tokens, account/device metadata or traffic patterns.

Configured AI providers receive only the current task or Agent-stage context from that device, plus the uploaded image for a photo task. Transmission happens when the user invokes generation and, after the user has explicitly enabled and configured a provider, may also happen when new Today facts trigger the documented debounced draft refresh. API keys never enter Domain data, Portable Data or sync.

## Addressed threats

| Threat | Control |
|---|---|
| Sync database/blob disclosure | Client-side AES-256-GCM; server stores opaque IDs and ciphertext |
| Cross-entity ciphertext substitution | Account/remote ID/key version bound in AAD |
| Tamper/truncation/wrong recovery key | GCM authentication and checksummed recovery format |
| Lost/repeated HTTP response | Idempotent operation IDs and rotating refresh tokens |
| Concurrent edits/data loss | CAS plus parent-aware three-way merge; conflicts retain both siblings |
| Deleted-vs-edited race | Explicit conflict, no timestamp winner |
| Stolen refresh token replay | Refresh rotation and previous-token reuse revocation |
| Compromised/retired device | Immediate device token revocation; optional full key rotation |
| Malicious archive | Authenticated container, hashes, path/size/ratio/reference validation, temporary restore |
| Secret leakage through export/backup | Secret fields excluded; Android wrapped-secret preference excluded from backup |
| Public self-host registration abuse | `first-user` default; configurable `open`/`closed` |
| Resource exhaustion | Configurable batch/page/entity/blob/account quotas and hard protocol ceilings |

## Residual risks

- A compromised unlocked client can read local data and keys, issue valid encrypted writes, capture screenshots or submit content to an AI provider.
- The server observes accounts, devices, IPs, timing, sizes and traffic volume. E2EE is not traffic-analysis resistance.
- Rollback of both local state and server backup to an internally consistent old version is not independently detected by an external transparency log.
- Losing every authorized device and the recovery string permanently loses encrypted data. A password reset cannot help.
- Availability, deletion of the only server copy and operator backup quality remain operator responsibilities.
- Metadata and plaintext may exist in OS swap, crash dumps, accessibility services or user-created screenshots outside application control.
- During a Python Portable Data apply, sibling staging/rollback directories beside `MEALCIRCUIT_HOME` temporarily contain local SQLite/config snapshots and managed assets in plaintext. They inherit local user permissions, are removed after commit/recovery, and are protected only by the same OS boundary as the live local profile.

Report vulnerabilities through the repository host's private advisory flow using only synthetic data. Tagged releases are blocked while that channel is disabled; never move vulnerability details to a public issue, and do not publish exploit details containing real health records.
