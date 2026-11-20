# crewkit

crewkit is a Python library for running Claude agents under a billing gate. Its main entry,
`crewkit.agent_sdk.connect`, starts a Claude Agent SDK client and yields it only after the Claude
Code CLI has reported which account it is about to bill and that account matches the backend you
asked for: an Anthropic API key (`api`) or your own Claude sign-in (`plan`). On a mismatch it raises
before any prompt is sent. The same package carries an optional Databricks AI Gateway route for that
client, and the runtime the account's CrewAI crews share: model tiers, knowledge inlining, a
config-driven crew builder and the installer that links a crew's skills into Claude Code.

## Install

crewkit is installed from its git tag; it is not on PyPI.

```sh
uv add "crewkit[agent-sdk] @ git+https://github.com/deliciousmonster/crewkit@v0.16.1"
# or
pip install "crewkit[agent-sdk] @ git+https://github.com/deliciousmonster/crewkit@v0.16.1"
```

Python 3.11 or newer, on macOS or Linux. The `agent-sdk` extra pins `claude-agent-sdk==0.2.183`,
which bundles Claude Code 2.1.326; add `databricks` for the gateway route and `run` for the CrewAI
path.

## Quickstart: one gated call

```python
import anyio
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from crewkit.agent_sdk import AgentSdkError, connect

options = ClaudeAgentOptions(
    tools=[],                   # no built-in tools
    setting_sources=[],         # no user, project or local settings
    strict_mcp_config=True,
    permission_mode="dontAsk",
    max_budget_usd=0.10,        # required under the api backend
)


async def main() -> None:
    try:
        async with connect(options, backend="api") as (client, verdict):
            print(verdict.backend, verdict.route, verdict.cli_version)
            await client.query("Name one prime number.")
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    print(message.result)
    except AgentSdkError as err:
        print("not run:", err.reason)


anyio.run(main)
```

Export `ANTHROPIC_API_KEY` first, or pass `key_file=Path(...)` to `connect`. The key goes to the
Claude Code child alone and leaves this process's environment once the gate passes. The block above
is run by `tests/test_readme.py` against a stand-in CLI, so a change to `connect` that breaks it
fails the suite.

`connect` raises `Refused` when policy stops the run and `Unavailable` when the CLI doesn't answer,
both subclasses of `AgentSdkError` whose text is a reason code and nothing else:

| Reason | Raised when |
|---|---|
| `backend_invalid`, `plan_not_pinned` | `CREW_LLM_BACKEND` is unknown, or `auto` would fall to the plan |
| `route_invalid`, `route_needs_backend`, `route_not_built:api`, `databricks_gateway_unconfigured` | `CREW_LLM_ROUTE` can't be taken as set |
| `options_unsafe:<field>` | an option would bring back built-in tools, settings or another route |
| `keep_env_forbidden:<name>` | `keep_env` names a credential, provider, proxy or Node variable |
| `no_key`, `empty_key`, `key_unreadable:<errno>` | `api` has no usable key |
| `account_missing`, `account_shape`, `provider:<name>`, `api_key_present`, `token_present`, `not_subscription`, `subscription_in_api_mode`, `cli_version_unknown` | the CLI's account report doesn't match the backend |
| `connect_timeout`, `cli_failed:<error>` | `Unavailable`: the CLI didn't start or answer within `timeout_s` |
| `databricks_gateway_*` | `DatabricksGatewayUnavailable`, before any child starts (see below) |

What `connect` does not do: bound a turn (it bounds only the start, `timeout_s`), fall back from
the gateway to the direct route, or stop Claude Code from reading managed settings or an Anthropic
profile under the `HOME` it keeps. While the SDK starts the child, `os.environ` holds only the
allowlist, for every thread in the process; call `connect` before starting threads that read or
write the environment.

The rest of this page documents each module.

## Modules


| Module | What it does |
|---|---|
| `crewkit.llm` | `llm(models, tier, key_file)`: the model for a tier from `models.yaml`, on the backend `CREW_LLM_BACKEND` picks |
| `crewkit.plan_llm` | `ClaudePlanLLM`, a CrewAI LLM that answers through `claude -p`, so a run bills the signed-in plan |
| `crewkit.agent_sdk` | `connect(options, ...)`: a Claude Agent SDK client, yielded only once the CLI's account report matches the backend (see "The Agent SDK path") |
| `crewkit.databricks_gateway` | `preflight(gw)`: the Databricks token and relayed-service checks for `CREW_LLM_ROUTE=databricks-gateway` |
| `crewkit.agents` | Claude Code's side of a crew: the model a tier names, the tools a role's capabilities bind, an agent file's frontmatter (with no `tools:` allowlist when given None, so a role's tools come from the policy the guard reads each call), `policy()`, the role policy a guard hook reads, and for delegation `label(flow, target)`, `target_of(report)`, `LEAD_DELEGATION` and `SUBAGENT_TARGET` (see "Agent labels") |
| `crewkit.guard` | Standard library only, for guard hooks: `rule_matches` (a settings.json permission rule against a tool call), `in_workspace`, `held`, `denial` (the never-list in a workspace, a role's subagent spawn unless it is parallel and spawns its own role, then any tool outside a role's own, then the consequential rules a role's capabilities lack) and `deny` (the hook's JSON) |
| `crewkit.config` | `CrewConfig`: a crew's `agents.yaml`, `tasks.yaml`, `crew.yaml`, `models.yaml` and `tools.yaml`, and each role's knowledge |
| `crewkit.builder` | `build_crew(cfg, task_names, inputs)`: one agent per role and tier, the manager from `crew.yaml` holding only delegation with the run's inputs filled in, a task whose context is not in the run reading the earlier outputs, tools from `tools.yaml`, each class once per agent |
| `crewkit.cli` | `main(cfg)`: `[--dry-run] <inputs in crew.yaml order> <task> ...` |
| `crewkit.workspace` | A crew's workspace, `<workdir>/agents/<crew>/`: `init` copies the crew's `templates/workspace/` without overwriting (filling `{{vcs}}`), `vcs_of` tells git, a synced drive or none, `policy` reads `workspace.yaml`'s `vcs` block, `find_records` lists older agent records under a folder |
| `crewkit.commands` | `dispatch(commands, argv, prog)`: one console script per crew, each tool a subcommand run as its module's `__main__`, so an agent writes `employmentcrew scouts run ...` literally every call |
| `crewkit.lint` | `aliases(text)` and `check_skills(root)`: the skill lines that name a command alias, keep a multi-word command in a variable, or run a variable as a command (zsh does not word-split one), for a crew's tests; `flow_tools(text)`, `check_flow_tools(tools, flows, roles)` and `flows_of(crew, path, root)`: each tool a role's flows declare on a `**Tools:**` line that the role does not hold (see "Flow tool declarations") |
| `crewkit.install` | `crewkit install | uninstall | doctor | update | lint`: link a crew's skills from its clone into Claude Code, driven by the crew's `[tool.crewkit]` table, and check its flows' declared tools |
| `crewkit.spec` | `load_spec(root)` reads the `[tool.crewkit]` table into a `Spec`; `find_root`, `resolve_crew_path` and `doctor` find the clone and check the box |
| `crewkit.migrate` | `plan(folder, Layout(...), sources)` and `apply(plan)`: relocate a legacy workspace (`git worktree move` or `git mv`, leaving a link) or lift one kept inside the workspace up into it (`moves` names each entry's place; a folder placed inside another folds in file by file), aggregate records kept elsewhere, add missing template files, move `evidence-<date>/` under `evidence/<date>/` with links, normalize knowledge frontmatter, rewrite old paths; commits nothing. With the layout's `marker` in place and no legacy record left, a rerun plans nothing; a move or format between two spellings of one file (INDEX.md and index.md on a case-insensitive volume) renames it through a free name and never writes one over the other |
| `crewkit.sdlc` | The agent SDLC every crew follows (see "The agent SDLC"): `projects`, `status`, `next_gate`, `scaffold`, `approve`, `change`, `retire`, `line`, `guide`, and `crewkit.sdlc.lint.lint`; templates in `crewkit/templates/sdlc/`. `workspace.init(..., sdlc=True)` and `Layout(sdlc=True, sdlc_sources=...)` scaffold the deliverables a workspace lacks and move nothing |

`CREW_LLM_BACKEND` is `api` (CrewAI's Anthropic provider and `ANTHROPIC_API_KEY`), `plan` (`claude -p`,
with the key removed from the child's environment) or `auto`, the default, which takes the key when
one is set or filed and the plan otherwise.

On the plan backend, a reply in Claude's own tool-call markup (`<invoke name=...>`) is rewritten as the
`Action:` lines CrewAI parses, and a worker holding tools that gives a final answer before any tool
result is asked once to call a tool first. `CREW_PLAN_DEBUG=<file>` appends one JSON line per
`claude -p` call: the agent, the model, `session` (`new`, `reused`, `retry` or `oneshot`), `sent` (the
characters sent), the wall time and the CLI's own `duration_ms` and `duration_api_ms`, so process
startup can be told from model time, then what was sent and the reply. A reused process reports
`duration_api_ms` summed over its life.

Each conversation keeps one `claude -p` process in stream-json mode, and a call sends it only what
CrewAI added since its last reply, usually the tool's `Observation:`. That saves a process start
per call: on a three-lookup CrewAI task on 2026-10-27, startup overhead fell from 1.24 s to 0.14 s a
call and later calls sent the process a few hundred characters instead of the whole history. A
process is reused only while the reply it remembers is the one CrewAI kept; a reply that wrote past a
stop word, or was rewritten from tool-call markup, starts a new process with the full history. At
most eight idle processes are kept, and all close when the run exits. `CREW_PLAN_PERSIST=0` restores
one process per call.

## The agent SDLC

Every crew delivers a project through five documents in its workspace: `brief.md` (what to
deliver, in the owner's words), `interview.md` (the agent's questions and the owner's quoted
answers), `requirements.md` (`REQ-n` entries with success and acceptance, signed by the owner),
`design/` (an index, one doc per component, and review rounds in `feedback.md`) and
`development.md` (`TASK-n` rows tracked to done, and acceptance per requirement). The crew keeps its
own roles and tasks; crewkit owns the documents, the three owner gates and the lint.

```sh
crewkit sdlc init            # write the deliverables the workspace lacks; never overwrites
crewkit sdlc status          # the stage, each gate and what is next
crewkit sdlc lint --strict   # every check; warnings fail too
```

The stage is computed from the files: brief, interview, requirements, design, development,
acceptance, done. `crewkit sdlc approve <requirements|design|acceptance> --by owner --words "..."`
records a gate as a row in the document's `## Sign-off` table with a hash of what was approved, and
`lint` reports an edit made after it. A change after sign-off goes through `crewkit sdlc change`
and `crewkit sdlc approve CR-<n>`, which bumps the requirements' `rev`. `crewkit sdlc retire <path>`
moves a superseded plan to `archive/pre-sdlc/` and rewrites references to it outside `journal/`,
`evidence/` and `archive/`. `crewkit sdlc guide` prints the whole process in one page.

Every command reads only the deliverables, at the workspace root or under `projects/<slug>/`; a test
runs `status` and `lint` beside 30,000 files in `evidence/` and checks that none is opened. Exit
codes are 0 clean, 1 findings or a refusal, 2 usage. A crew adds `"sdlc": "crewkit.sdlc"` to its
`crewkit.commands.dispatch` table to offer `<crew> sdlc ...`.

crewkit cannot tell an owner's words from an agent's claim. A crew lets only its Lead run `approve`,
after the owner has answered; the hash makes a later silent edit visible, not a forged approval
impossible.

## The `run` extra

The core needs only `pyyaml`: `import crewkit`, `crewkit.workspace`, `crewkit.migrate`, `crewkit.sdlc`, `crewkit.spec` and `crewkit.install` never import CrewAI. CrewAI and crewai-tools (pinned, Python below 3.14) are the extra `crewkit[run]`, needed for `crewkit.builder`, `crewkit.cli` and `crewkit.plan_llm`, the headless path. `crewkit.agent_sdk` needs `crewkit[agent-sdk]` and `crewkit.databricks_gateway` `crewkit[databricks]`; both import their SDK only when called. A crew depends on plain `crewkit` and offers its own extra, for example `examplecrew[run] = ["crewkit[run]"]`.

## The Agent SDK path

`crewkit.agent_sdk.connect(options)` is an async context manager over `claude_agent_sdk.ClaudeSDKClient`,
in the extra `crewkit[agent-sdk]`, which pins `claude-agent-sdk==0.2.183` (Claude Code 2.1.326). It
resolves the backend, runs the Claude Code child with only `HOME`, `USER`, `LOGNAME`, `PATH`,
`TMPDIR`, `LANG`, `TZ` and the names in `keep_env` inherited, hands an API key to that child alone, and
asks it which account it is about to bill before yielding `(client, verdict)`. A report that doesn't
match the backend, in either direction, raises `Refused` with a reason code, and no prompt is sent;
a CLI that doesn't answer raises `Unavailable`. Both are ordinary exceptions whose text is the code.
The options must name `tools` as a list, `setting_sources=[]`, `strict_mcp_config=True` and
`permission_mode="dontAsk"`, carry no `settings` or `extra_args`, and in `api` set `max_budget_usd`.
`os.environ` is reduced only while the SDK starts the child, so call `connect` before starting threads
that read or write the environment. An exported `ANTHROPIC_API_KEY` stays removed once `connect` has
yielded; a refusal or an `Unavailable` before then puts it back, so the call can be retried. The one
`CLAUDE` name `options.env` and `keep_env` may carry is `CLAUDE_CODE_MAX_RETRIES`, which sets how many
times the CLI retries a failed API request inside your turn.

crewkit runs the Agent SDK on one of two credentials. `CREW_LLM_BACKEND=api` uses an Anthropic API
key, the authentication the Agent SDK documents for anything you deploy, and `auto` (the default)
means `api` on this path: with no key, it refuses rather than fall back. `CREW_LLM_BACKEND=plan`, set
by name, uses the Claude subscription Claude Code is signed in to, for your own use on your own
machine and nothing you offer to others. Anthropic's Agent SDK overview says: "Unless previously
approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for
their products, including agents built on the Claude Agent SDK. Use the API key authentication
methods described in the Quickstart instead." Either way, crewkit checks which account the CLI
reports before sending a prompt, and refuses on a mismatch.

`CREW_LLM_ROUTE=databricks-gateway`, with `CREW_LLM_BACKEND=plan`, sends that same sign-in through a
Databricks Unity AI Gateway model provider service in relayed mode, so each call is logged in the
workspace's `system.ai_gateway.usage`. It doesn't change what the sign-in is for: your own use on your
own machine, under the note above. Set `CREW_DATABRICKS_PROFILE` to a `~/.databrickscfg` profile and
`CREW_DATABRICKS_SERVICE` to the service's full name, and install `crewkit[agent-sdk,databricks]`.
Before each session crewkit fetches a Databricks token through that profile, checks the service is
relayed (so it can't bill an API key behind your back) and serves models, and hands the token to the
Claude Code child alone, where other processes of your user can read it for the child's life. If any
of that fails, `connect` raises `DatabricksGatewayUnavailable` before anything is sent (a service name
other than `<catalog>.<schema>.<name>` of letters, digits, `_` and `-` is
`databricks_gateway_service_invalid`, and a redirect from the workspace is
`databricks_gateway_unreachable`, never followed with the token); going direct
is your program's choice, with `connect(..., route="direct")`. Databricks documents this mode for
`ug claude` run by a developer; crewkit's SDK route is tested against it but is not that documented
client. The route applies to `crewkit.agent_sdk` only; the CrewAI path ignores it.

The account report is undocumented in part (`apiProvider`), so the gate matches the exact key set
Claude Code 2.1.326 reported on each route, recorded under `tests/fixtures/accounts/2.1.326/`; a CLI
that adds or drops a key refuses until its shapes are recorded. `CREWKIT_LIVE=1` runs
`tests/test_agent_sdk_live.py` against the real CLI with no prompt sent.

## Installing a crew's skills from a clone

A user with access to the crew's repo clones it anywhere, tells their Claude Code agent "install the skill", then runs `/<skill> <target dir>`. The mechanics live here; the crew declares what it ships in its own `pyproject.toml`:

```toml
[tool.crewkit]
skills = ["employ"]            # required: folders under skills_dir, each holding a SKILL.md
name = "employmentcrew"        # default: [project] name; names ~/.config/<name>/ and <NAME>_HOME
skills_dir = "skills"          # default
repo = "owner/employmentcrew"  # named in install errors, so a missing GitHub access is clear
agents_dir = "agents-claude"   # optional: *.md copied to .claude/agents/, {crew_path} substituted
hook = "uv run --project {crew_path} python -m crew status"  # optional SessionStart command; in a hook,
                                 # {crew_path} goes in shell-quoted, so the template leaves it unquoted

bin = ["<console script>"]        # optional: a launcher in ~/.local/bin (or $CREWKIT_BIN) running
                                 # `uv run --quiet --project <clone> <script> "$@"`; doctor warns when it is off PATH;
                                 # the first is the command every hint names (without one, `uv run --directory <clone> crewkit`)
[tool.crewkit.flows]             # optional: what `crewkit lint` checks
tools = "src/<crew>/config/tools.yaml"   # the capabilities, as crewkit.agents reads them
crew = "src/<crew>/config/crew.yaml"     # roles.<role>.claude_code.flows names each role's flows
path = "skills/<skill>/flows/{flow}.md"  # where a flow's file is, {flow} its name

[[tool.crewkit.hooks]]           # optional, any number: any Claude Code hook event, with an optional matcher
event = "PreToolUse"
matcher = "Bash"
command = "python3 {crew_path}/guard.py pre"
permissions = ["Bash(uv run *)"]  # optional settings.json permissions.allow entries, {crew_path} substituted

[[tool.crewkit.hooks]]           # the cycle's usage line after every reply (crewkit 0.15.0 or later)
event = "Stop"
command = "{crew_path}/.venv/bin/python -I -m crewkit.stopline <skill> || true"

[[tool.crewkit.prerequisites]]   # optional; level is "hard" (fails doctor) or "soft" (warns)
name = "poppler"
binaries = ["pdftotext", "pdfinfo"]   # any one on PATH satisfies it; omit for a manual note
level = "soft"
hint = "brew install poppler"
```

```
crewkit install   [--root CLONE] [--scope user|project] [--target DIR] [--force]
crewkit uninstall [--root CLONE]
crewkit doctor    [--root CLONE]
crewkit update    [--root CLONE] [--force]
crewkit lint      [--root CLONE]
```

`lint` checks each role's flows against the tools the role holds (see "Flow tool declarations"), prints one `[FAIL]` row per problem and exits 1 on any. It needs the `[tool.crewkit.flows]` table.

`update` reports, and changes nothing: whether the crew's clone is current, ahead, behind or diverged from its upstream (`git ls-remote`, no fetch, so when the remote's tip is a commit the clone hasn't fetched it can't tell behind from diverged, and says the remote has moved on since the last fetch and names the fetch that tells which), and whether the crewkit tag the crew's `[tool.uv.sources]` pins is older than crewkit's newest tag; a pin that is not an x.y.z tag, or a remote with none, reads `not checked`, since there is nothing to compare. The first line is `update: something is behind`, `update: up to date`, or, when part of the check could not run, what it did and did not look at (`update: the crewkit pin not behind; the clone not checked`), and a later line names the step to take where there is one to name (a diverged clone is the owner's to reconcile); moving the pin stays a crew change (bump, `uv lock`, tests, `install`). The answer is kept in `~/.config/<crew>/update-check.json` for the rest of the day while the clone's commit and the pin are the ones it checked, so a pull or a new pin asks again (`--force` asks again too); a check that can't reach the remote says `not checked` and is not kept. A kept answer that is JSON but not an object, today's answer without a list of lines, one that can't be read, and a remote that no longer has the branch the clone tracks each stop with one line and exit 1, writing nothing (`--force` replaces a kept answer); text that is not JSON at all, such as a write cut short, is asked again and replaced, like an answer from an earlier day, another clone, another commit or another pin. An answer that can't be written is noted under the report. A crew's skill runs it first thing in a session.

Run from the clone, `--root` is found by walking up to the nearest `pyproject.toml` with `[tool.crewkit]`. A crew exposes the same through its own CLI with `sys.exit(crewkit.install.main(argv, root=CREW_ROOT))`, where `CREW_ROOT` comes from the crew package's real path (`Path(__file__).resolve()`), never from skill text.

`install` runs `doctor` (hard: uv, git, Python 3.11 or newer; soft: what the crew declares, and, when a hook runs `crewkit.stopline`, that the crew's `.venv` imports it, ``[warn] stopline hook - run `uv sync` in <clone>`` when not; exit non-zero only on a hard miss), writes `~/.config/<crew>/crew-path`, and symlinks `<crew>/skills/<name>` into `~/.claude/skills/` (`--scope user`, the default) or `<target>/.claude/skills/` (`--scope project`). A link already pointing at the clone is left alone; any other existing path stops the install unless `--force`, which moves it aside as `<name>.bak-<timestamp>`. Every link, agent file and launcher (a link to nothing counts as one that is not ours) is checked, and `agents_dir` with them, before the first one is made: each that conflicts is listed in one `conflicts found, nothing installed:` message in the doctor's `[FAIL]` form, and a file where a folder must be made (which `--force` does not move) is listed with them. Agents, hooks and permissions apply only when declared; `settings.json` is merged after a timestamped backup. When the crew declares hooks or permissions, or an earlier install recorded settings entries, `install` and `uninstall` read `settings.json` and every other installed crew's `install.json` before they change anything; a `settings.json` that is a link to nothing or not a JSON object stops either one with nothing installed or nothing removed, and so does one that holds the wrong type where the crew reads or writes it (`hooks` not an object, a hook event the crew declares or recorded not a list, `permissions` not an object, `permissions.allow` not a list), listed as `[FAIL]` rows under `conflicts found, nothing installed:` or `nothing removed:`; a part the crew never touches is not checked. The crew's own `install.json` is read first, every time, and a link to nothing, text that is not a JSON object, or a list there in a shape `install` never writes (`links: null` after a hand edit) stops either one the same way. Another crew's `install.json` is read as far as it parses: a missing or null list counts as empty, a hook record with no group protects nothing, and the output names what it skipped. `~/.config/<crew>/install.json` records each link with its target, each agent file with a hash of the text written, and only the hooks (each with its event) and permissions the install added. A later `install` removes a recorded hook group or permission the crew no longer declares, so a changed matcher replaces its group instead of joining it; an entry the user had stays, and so does one another installed crew's `install.json` still records (a crew that declares an entry already present records it too, so the other dropping it leaves it). `uninstall` removes a link only while it still points at the clone, an agent file only while its text is unchanged, and only the settings entries recorded; an entry the user already had stays. It names what it leaves, including any path `--force` moved aside; it also leaves the stop line's state under `~/.cache/crewkit/stopline/`. Only the hook prunes that folder, so once the last crew declaring it is uninstalled nothing does; remove it by hand with `rm -r ~/.cache/crewkit/stopline` (under `$XDG_CACHE_HOME` when that is set). A second run changes nothing. macOS and Linux only; Windows is unsupported.

A crew's repository may be private. Without access, `uv run` fails while fetching it, before crewkit starts, so the access message comes from uv and git; each crew's Quickstart says so before the first command.

A skill finds the crew in this order: `$<CREW>_HOME` (name upper-cased, `-` as `_`), then `~/.config/<crew>/crew-path`, then two levels up from the real path of the skill's own directory. `crewkit.install.resolve_crew_path(crew, skill_dir)` implements it.

A crew depends on it by tag:

```toml
[tool.uv.sources]
crewkit = { git = "https://github.com/deliciousmonster/crewkit", tag = "v0.16.1" }
```

## Flow tool declarations

A flow is a Markdown file a role's subagent follows, named in `crew.yaml` under
`roles.<role>.claude_code.flows`. Each flow says what tools it asks of the role that runs it, on a
`**Tools:**` line with every tool name in backticks, as `tools.yaml` binds them:

```markdown
**Tools:** `Read`, `Grep`, `Glob`, `Edit`, `Write`, `Bash`
**Tools (lead):** `WebSearch`, `WebFetch`, `mcp__claude-in-chrome__navigate`
```

An unscoped line binds every role that runs the flow. `**Tools (<role>, ...):**` binds only the roles it
names, for the steps a flow gives one of them, such as the Lead's steps in a flow a subagent also reads.
`**Tools:** none.` declares that the flow needs no tool. A declaration runs on until a blank line, a list
item, a heading or the next `**...:**` line, so it may wrap. A name ending in `*` held by the role covers any
tool it prefixes.

`crewkit.lint.check_flow_tools` fails on each tool a role's flows ask for that the role's capabilities do not
bind, naming the role, the flow file and the tool, and on a flow that is missing, declares nothing, lists names
without backticks, or scopes a line to a role the crew does not have. It reads only the declaration, never the
prose, so it is deterministic; it cannot tell that a step's prose asks for a tool the declaration leaves out,
which is the flow author's to keep in step. A crew's renderer calls it beside rendering, so `render --check`
fails with it, and `crewkit lint` (or `<crew> lint`, through `crewkit.install.main`) runs it alone:

```python
from crewkit import lint
problems = lint.check_flow_tools(tools, lint.flows_of(crew, "skills/employ/flows/{flow}.md", ROOT), list(crew["roles"]))
```

This came from 2026-10-29, when the employment crew's tailor followed its flow's instruction to search the web
and the guard denied WebSearch, which its role does not hold.

## Agent labels

Claude Code labels each notification from a background agent with the description the Agent tool was given
when it was spawned, and keeps that label for the agent's life. A SendMessage that continues the agent for a
different target changes nothing crewkit or a hook can reach: on 2026-10-29 an agent spawned as "Apply: Netflix
Agent Platform SEM" was continued for Asurion and then Parallax, and every notification still read Netflix.
crewkit cannot relabel an agent. What it gives a crew to render:

- `LEAD_DELEGATION`, rules for the Lead's skill: one agent per target, spawned with `label(flow, target)`
  (`Apply: Asurion - Staff SEM`, one line, at most 60 characters); SendMessage only to continue an agent on the
  target it was spawned for, opening with `Target: <target>` and with `summary` set to the same; and a
  report's first line, not the notification's title, as the truth about which target it covers.
- `SUBAGENT_TARGET`, a rule for every role's brief: each report opens with `Target: <target>`, and a
  continuation for a different target says so on its second line.
- `target_of(report)`, the target a report's first line names, for a Lead's tooling or a crew's tests.

The title of a notification can still be stale when a Lead breaks the rule; the first line of its body is not.

Two task keys beside CrewAI's schema change how a task builds. `runtime: claude_code` marks a task
that needs a signed-in browser or the owner's approval; the builder skips it and says so. `gate: <name>`
names a guardrail factory the crew passes to `main(cfg, gates={...})`: it takes the run's inputs and
returns the CrewAI guardrail, so a failed check sends the task back to its agent. A role's
`inline_sections` in `crew.yaml` keeps only the named `## ` sections of a knowledge file.

Tests: `uv run python -m unittest discover -s tests` runs the core and skips the CrewAI tests; `uv run --extra run --python 3.13 python -m unittest discover -s tests` runs all of them.

## Token use and API price

`crewkit.usage` reads Claude Code's transcripts for a folder and reports what a run used: each API call
once (Claude Code writes one row per content block, each with the whole call's usage), its context, its
tokens and its price at API list rates by model (`PRICES`: input, 5-minute and 1-hour cache writes,
cache reads, output; fast mode doubled). `report(session, turn, command=, budgets=)` covers the last
turn, every turn, or the cycle since a command such as `/employ`; `line(rep)` is the cycle's closing
line, `Tokens this cycle: 54.1k · API price: $0.02`; `by_key([rep, ...])` totals the subagents whose
label names a key, a Linear ticket by default (`Build: YTC-2976`). The price is what the calls would
cost on the API, not what a Claude plan bills.

A report selects every agent its window spawned at any depth: one named by an `Agent` call or result in
the window, and one whose `parentAgentId` names a selected agent. One set of call ids spans the whole
report, the Lead's window and every selected agent, so a call a fork replays from its parent counts once,
where it is first visited; under `"all"` that set spans the session. A call is keyed by its message id,
`requestId` or `uuid`, else by its file and byte offset, and rows split on newlines only, since JSON
leaves U+2028 unescaped. Each call is priced in integer nano-dollars, `nanos` beside every `usd`
(the Lead, each agent, the total and each `by_key` row), and `usd` is `cycle_usd(nanos)`, so a ticket's
figure and the cycle's line over the same calls print the same cents. Against 0.14.1, totals rise on
sessions with depth-2 agents, and a per-agent or per-ticket figure can fall where a fork's replayed
calls now count in the window that first visits them. `session_path(id)` returns the one transcript
named by a session id, so a crew's command can take `--session` instead of guessing by mtime.

`python -I -m crewkit.stopline <skill>` is that line as a Claude Code `Stop` hook: after each reply in a
cycle started by typing `/<skill>`, it prints `{"systemMessage": "Tokens this cycle: …"}`, counted by
the rule above over the Lead and every selected agent. It reads only the bytes appended since its last
run, keeping its place, its call ids and its sums in `~/.cache/crewkit/stopline/<session>-<skill>.json`
(`$XDG_CACHE_HOME` when set; mode 0600, no prompt or reply text), and recounts from the start when the
transcript was replaced or rewritten. It prints nothing before a typed `/<skill>`, after another
installed crew's skill was typed later, when the reply's last call is not yet in the transcript after
three tries 100 ms apart, or past its 1.5 s budget (a 3 s alarm ends it outright); it never fails the
hook. The command names the crew's own interpreter with `-I`, so the folder Claude Code runs it in
can't shadow `crewkit`, and `|| true` keeps a crew without a `.venv` silent. A crew pinned below 0.15.0
has no hook; `crewkit doctor` warns when the crew's `.venv` can't import it. Through the crew's own
launcher `uv run` syncs `.venv` first, so the row reads `[warn]` mainly from `crewkit doctor --root <clone>`.
