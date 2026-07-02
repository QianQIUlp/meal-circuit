# Security

MealCircuit `v0.1` is a local, single-user application. It has no account system, authentication layer or TLS termination.

- Keep the default loopback binding.
- Do not expose `--allow-remote` directly to the internet.
- Treat the database, images, contexts and backups as sensitive health data.
- Run `python tools/release_check.py` before publishing changes.

Report vulnerabilities through the repository host's private security advisory feature. Do not include real personal data in a report.
