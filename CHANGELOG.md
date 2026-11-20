# Changelog

Each release is an annotated git tag, `vX.Y.Z`; a consumer pins it in `[tool.uv.sources]`. The
"Consumers" line says what moving a pin to that release changes. Entries before 0.16.0 are
summarised from the tag messages and release commits.

## 0.17.1 (2026-11-20)

A patch release: the denylist's structural check learns what a repository may name in public.

- `.crewkit-allow` at a repository's root: one case-insensitive regex per line, `#` comments,
  searched in each email address and home-directory path the structural check finds, so the public
  sender addresses a mail filter names pass. It never applies to the private terms, and the file is
  scanned for them like any other. An entry that allows nothing prints as
  `denylist: note: .crewkit-allow line N allowed nothing` and does not fail the run; a bad regex
  exits 2.
- A `noreply` or `no-reply` address passes with no entry.
- `denylist.report(root)` returns the hits, the skip reason and the notes; `check` is unchanged.

Consumers: a pin and lock change. A repository whose check flagged public vendor addresses can
commit a `.crewkit-allow` for them; nothing else moves.

## 0.17.0 (2026-11-20)

A minor release: the public API grows and no name changes.

- `crewkit.sdlc`, the agent SDLC every crew follows: five deliverables per project (`brief.md`,
  `interview.md`, `requirements.md`, `design/`, `development.md`) at the workspace root or under
  `projects/<slug>/`, the stage computed from them, and three owner gates recorded as `## Sign-off`
  rows with a hash of what was signed. `status`, `next_gate`, `scaffold`, `approve`, `change`
  (change requests, which bump the requirements' `rev`), `retire` (to `archive/pre-sdlc/`, with
  each reference to the moved file rewritten outside `journal/`, `evidence/` and `archive/`), `line` for a SessionStart
  hook, `guide`, `SDLC_MAP` for a crew's workspace map, and `crewkit.sdlc.lint.lint`.
- `crewkit sdlc status | next | lint [--strict] [--json] | init | approve | change | retire | guide`;
  a crew offers it as `<crew> sdlc` with one `crewkit.commands.dispatch` line,
  `"sdlc": "crewkit.sdlc"`. Exit 0 clean, 1 findings or a refusal, 2 usage (a `--project` that does
  not exist or is not a plain name included).
- `approve` refuses a gate before its stage (G1 needs the brief ready and the interview closed, G2
  approved requirements, G3 every task done or dropped and every `must` passing) and while lint has
  an error. Requirements are approved once; later edits go through `change` and `approve CR-<n>`,
  and `change` refuses a REQ that does not exist.
- Lint errors: a deliverable missing for the stage, a required heading missing, an unknown status, a
  gate signed while the project is at an earlier stage, a sign-off with no words, a signed section
  edited since (for requirements, unless a proposed change request names every REQ whose text
  changed; G1 records a digest per REQ in `signed_reqs`), front matter, a rev or a shared copy
  that cannot be read (`unreadable`), a duplicate id, a citation of a REQ that
  does not exist, an interview marked complete with a question open. Warnings: a requirement without
  Success or Acceptance, a `must` requirement no design doc or no task cites, a design doc stale
  against `requirements_rev`, a brief changed since the interview's `brief_hash`, a `done` task with
  no evidence on disk, open feedback on an approved design, a task with no ticket when a tracker is
  set, a `shared_with` copy that differs.
- Templates for the five deliverables and a one-page `GUIDE.md` ship in
  `crewkit/templates/sdlc/`.
- `workspace.init(..., sdlc=True)` writes the deliverables the crew's own template lacks, never over
  a file. `migrate.Layout` gains `sdlc` and `sdlc_sources`; with `sdlc`, `plan` adds a `scaffold`
  step per missing deliverable and a review line per draft source it finds, and moves nothing.
- Every command reads only the deliverables; a test builds 30,000 files under `evidence/` and checks
  that `status`, `lint` and `retire` open none of them.
- `crewkit.denylist` and `crewkit denylist [--dir]`: tracked files (not `uv.lock`) against a
  private-terms list kept outside the repository (`$CREWKIT_PRIVATE_TERMS` or
  `~/.config/crewkit/private-terms`), reported as `path:line` and the term's line in the list, never
  the term; a file whose name holds a term is reported as `file #N`, its index in `git ls-files`.
  A pattern that matches empty text is refused. Skipped with the reason when no list is installed. A structural check needs no list and
  flags home-directory paths and email addresses outside the example and noreply domains.
  `assert_clean(test, root)` is a crew's three-line test. CI runs the term scan when the
  `PRIVATE_TERMS` secret is set.
- Examples, docstrings and test fixtures use generic crew, skill and command names.

Consumers: a pin and lock change. Both `sdlc` switches default to off, so a crew that passes neither
is untouched; one that passes `sdlc=True` gets the five documents scaffolded on its next init or
migrate. A crew may add the three-line `denylist.assert_clean` test.

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
