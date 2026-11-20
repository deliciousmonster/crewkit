# Recorded fixtures

`accounts/<cli version>/` holds the `account` block of the initialize answer the bundled Claude Code CLI
gave on each route, zero prompts, with `email` and `organization` replaced by neutral values;
`initialize.json` holds the answer's key names. `CREWKIT_LIVE=1` runs `tests/test_agent_sdk_live.py`,
which fails when the live shapes differ and prints the command that re-records them.

`databricks/<date>/` holds a model provider service in relayed mode and the gateway's models list, as
the REST API answered them, every string value but the model names replaced by its key path. The
endpoint and `config.anthropic.relayed` are undocumented; `test_live_databricks` in
`tests/test_agent_sdk_live.py` checks the live body still has these key paths.
