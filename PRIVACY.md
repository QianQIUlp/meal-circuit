# Privacy

MealCircuit stores its database, personal profile, settings, uploaded images, food labels, exports and backups outside the source repository under `MEALCIRCUIT_HOME`.

The application itself contains no telemetry and does not call an external model API. When a user asks Codex, Claude Code or another Agent to process a task, that Agent may transmit the exported context or image to its model provider. Users must review the provider's data policy before submitting health information.

Never attach a real database, context export, result file, personal doctrine, health log or meal photograph to an issue or pull request. Use synthetic records for reproduction.

Removing the source repository does not remove private data. Use `python -m mealcircuit.agent_cli doctor` to locate the private directory before deleting or backing it up.
