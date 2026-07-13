# Security

MealCircuit is local-first. Desktop and Android work without an account or network. Optional synchronization is end-to-end encrypted and the open-source server stores only opaque account/device metadata, encrypted revisions and encrypted attachment chunks.

- Keep the desktop Web service on loopback. `--allow-remote` is not an authentication or TLS layer.
- Custom sync URLs must use HTTPS; plain HTTP is limited to explicit localhost debug mode.
- Store the recovery string separately. Losing every authorized device and the recovery string makes remote data unrecoverable; resetting the login password does not decrypt data.
- Treat local databases, photos, `.mcx` exports, recovery strings and AI contexts as sensitive health information.
- API keys and sync secrets belong only in environment/process memory or the operating-system secure store. They must never enter Domain revisions, logs, Portable Data or sync bodies.
- Run the complete CI/release gates and `python tools/release_check.py` before publication.

The detailed model, controls and residual risks are in [`docs/threat-model.md`](docs/threat-model.md). Cryptographic code has cross-language vectors and tamper tests but has not received an independent third-party audit.

Report vulnerabilities through the repository host's private security advisory feature. Use synthetic data and do not attach a real database, export, context, recovery string or meal photo.
