# Changelog

Each release is an annotated git tag, `vX.Y.Z`; a consumer pins it in `[tool.uv.sources]`. The
"Consumers" line says what moving a pin to that release changes. Entries before 0.16.0 are
summarised from the tag messages and release commits.

## 0.16.2 (2026-11-20)

A patch release: no public name changes.

- `migrate.apply` renames a tracked knowledge file with `git mv` on every volume, so the index
  follows a format rename such as `INDEX.md` to `index.md` or `OldFact.md` to `old-fact.md`. It
  wrote the new name and unlinked the old one, which left the old name in the index as a deletion
  and the new one untracked; only a case-only rename on a case-insensitive volume took `git mv`.
- The update test reads `@{u}` after its pushes. Git 2.56.1 moves `origin/main` on a push to
  origin's URL given by URL, and 2.55 does not, so the test no longer assumes either. `update.check`
  still never fetches.

Consumers: a pin and lock change. A migration run in a git checkout now stages each tracked
knowledge rename, on any volume.

## 0.16.1 (2026-11-20)

A patch release: no public name changes, and every change is a fix or an addition.

- `agent_sdk.connect` keeps an exported `ANTHROPIC_API_KEY` in `os.environ` until the gate passes.
  In 0.16.0 it removed the key before its last refusal check, so a refusal or an `Unavailable`
  left the process with no key and a retry came back `Refused("no_key")`. The key still leaves
  for good once `connect` yields. A key put back after a refusal stays in `os.environ` for the
  life of the process unless a later `connect` passes, so every subprocess started meanwhile
  inherits it; 0.16.0 removed it on every path.
- `keep_env` is checked before the key is read.
- `CLAUDE_CODE_MAX_RETRIES` may be set in `options.env` or named in `keep_env`; every other
  `CLAUDE` name is still refused.
- The Databricks gateway's pre-check reads follow no redirect: a 3xx is
  `databricks_gateway_unreachable`, and the token's headers never reach another host.
- `CREW_DATABRICKS_SERVICE` must be `<catalog>.<schema>.<name>` of letters, digits, `_` and `-`;
  anything else is `databricks_gateway_service_invalid`, raised before the token is fetched.
- Packaging: MIT `LICENSE`, `py.typed`, a full `[project]` table (readme, licence, URLs,
  classifiers), and an sdist without `uv.lock` or `scripts/`.
- The README opens on what crewkit does, with the install line and a Quickstart that a test runs.
- CI on GitHub Actions: ruff, mypy and the tests on ubuntu and macOS, Python 3.11 and 3.13.
- `scripts/push.sh` pushes whether the repository is private or public.

Consumers: a pin and lock change. A caller that relied on the key being gone after a failed
`connect` now finds it back; a service name with any other character now refuses before the
token fetch.

## 0.16.0 (2026-11-17)

- `crewkit.agent_sdk.connect(options)` yields a `ClaudeSDKClient` only once the CLI's account
  report matches the backend crewkit resolved, matched on the exact key set Claude Code 2.1.326
  reported on each route. `plan` is taken only by name; `auto` means `api` there and refuses
  without a key. The child inherits an allowlisted environment, and an API key reaches the child
  alone. Refusals are `Refused` and `Unavailable` exceptions whose text is the reason code.
- `CREW_LLM_ROUTE=databricks-gateway` under a named `plan` sends the sign-in through a relayed Unity
  Catalog model provider service; `crewkit.databricks_gateway.preflight` checks the token, the
  relayed mode and the models list, and raises `DatabricksGatewayUnavailable` before any child
  starts.
- Extras `agent-sdk` (`claude-agent-sdk==0.2.183`, `anyio>=4.5`) and `databricks`
  (`databricks-sdk==0.167.0`, `anyio>=4.5`).

Consumers: a pin and lock change for a crew that imports neither new module; `llm.backend()` and
`llm.BACKENDS` behave as in 0.15.0.

## 0.15.0 (2026-11-17)

The cycle's usage line is printed by a Stop hook (`crewkit.stopline`).

## 0.14.2 (2026-11-17)

Usage counts nested agents, and each API call once.

## 0.14.1 (2026-11-05)

Usage: a transcript row with no message id is its own call.

## 0.14.0 (2026-11-05)

Usage reads transcripts once per API call and prices each call at API list rates, with a per-ticket
roll-up. Totals differ from 0.13.0's.

## 0.13.0 (2026-10-29)

Flows declare their tools and `crewkit lint` checks them against each role; one agent per target.

## 0.12.0 (2026-10-29)

`update` reads a remote tip the clone hasn't fetched as moved on since the last fetch; install and
update shell-quote the clone path in hooks and launchers.

## 0.11.0 to 0.11.2 (2026-10-28)

`<crew> update` reports a clone or crewkit pin that is behind, once a day, and changes nothing.
Install checks every link, agent file, launcher and `agents_dir` before it makes the first link, and
install and update refuse bad shapes before they change anything.

## 0.10.0 to 0.10.4 (2026-10-27 to 2026-10-28)

A parallel role may split its work across copies of itself, and no other spawn. A tool's string exit
is printed. Install prunes what an earlier install recorded and the crew no longer declares, keeps an
entry another installed crew still records, and reads `settings.json` and every other crew's
manifest before it changes anything.

## 0.9.0 (2026-10-27)

A role is held to its tools from the policy, so a new capability needs no restart.

## 0.8.0 (2026-10-27)

A launcher per console script, subcommand dispatch, and a skill lint for command aliases.

## 0.7.0 (2026-10-27)

Shared agent rendering and the role guard.

## 0.6.0 (2026-10-27)

Install hooks for any Claude Code event.

## 0.5.0 (2026-10-27)

One `claude -p` process per conversation on the plan backend.

## 0.4.0 (2026-10-16)

`scripts/push.sh` pushes as the account to the private origin only.

## 0.3.0 (2026-10-16)

`workspace` and `migrate`, shared by every crew.

## 0.2.3 (2026-10-16)

A shared builder for YAML-defined CrewAI crews.

## 0.1.0 (2026-10-15)

Model tiers, the API or Claude-plan backend, knowledge inlining and a config-driven crew builder for
CrewAI 1.15.28.
