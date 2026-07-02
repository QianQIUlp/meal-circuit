# Contributing

Run the following checks before submitting a change:

```powershell
.\test.ps1
python tools\release_check.py
```

Tests, screenshots, databases and examples must use clearly synthetic data. Do not submit real meal photographs, health metrics, local absolute paths, context exports or model results.

Keep the Python runtime dependency-free unless a dependency change has been discussed first. Preserve append-only correction and review-history behavior.
