# What the crews' guard hooks share, standard library only: matching a permission rule against a tool call,
# finding a workspace above a directory, and the deny a role policy (crewkit.agents.policy) gives.
import json
import re
from pathlib import Path

SPAWN = ("Agent", "Task")  # Claude Code's subagent tool, under its current and its earlier name
PREFIX = re.compile(r"^(?:\w+=\S*\s+|sudo\s+|env\s+|time\s+|command\s+|exec\s+|\(\s*)+")


def rule_matches(rule: str, tool: str, command: str = "") -> bool:
    """A permission rule as settings.json writes it, `Bash(git push *)` or a bare tool name."""
    m = re.fullmatch(r"(\w+)\((.*)\)", rule, re.S)
    if not m:
        return rule == tool
    if m.group(1) != tool or tool != "Bash":
        return False
    pattern = re.escape(m.group(2)).replace(r"\*", ".*")
    pattern = re.sub(r"\\ \.\*$", "(?: .*)?", pattern)  # `git push *` also matches a bare `git push`
    return any(re.fullmatch(pattern, PREFIX.sub("", part.strip()), re.S)
               for part in re.split(r"&&|\|\||;|\||\n", command) if part.strip())


def in_workspace(cwd: str, marker: str) -> bool:
    here = Path(cwd or ".")
    return any((d / marker).is_file() for d in [here, *here.parents])


def held(tool: str, names: list) -> bool:
    """Whether a tool is among names, where a name may end in * (`mcp__claude-in-chrome__*`)."""
    return any(tool == n or (n.endswith("*") and tool.startswith(n[:-1])) for n in names)


def denial(event: dict, policy: dict, marker: str, where: str = "tools.yaml") -> str | None:
    """The reason a policy denies this call, or None: the never-list for any agent in a workspace (found by
    `marker` above the working directory) or any named role; a role's subagent spawn unless it is parallel and
    spawns its own role; then, for a role whose policy lists its tools,
    any MCP tool it does not hold and any other tool a capability it lacks binds; then the rules of
    capabilities a role lacks."""
    tool, command = event.get("tool_name", ""), (event.get("tool_input") or {}).get("command", "")
    agent = policy["agents"].get(event.get("agent_type") or "")
    if agent or in_workspace(event.get("cwd", ""), marker):
        for rule in policy["never"]:
            if rule_matches(rule, tool, command):
                return f"`{rule}` is on the crew's never-list ({where}); no role does it."
    if agent and tool in SPAWN:
        wanted = (event.get("tool_input") or {}).get("subagent_type") or "general-purpose"
        if not agent.get("parallel"):
            return (f"The {agent['role']} does not run in parallel ({where}), so it spawns no subagents; do the "
                    f"work yourself, or report what should be split and the Lead decides.")
        if wanted != event.get("agent_type"):
            return (f"The {agent['role']} may spawn only its own role, `{event.get('agent_type')}`, to split its "
                    f"work ({where}); `{wanted}` is not it.")
        return None
    if agent and "tools" in agent and not held(tool, agent["tools"]) and (
            tool.startswith("mcp__") or held(tool, policy.get("bound", []))):
        return (f"The {agent['role']} holds no capability that binds `{tool}` ({where}). Report what you would do, "
                f"and the Lead does it or asks the owner.")
    for rule, capability in (agent or {}).get("deny", {}).items():
        if rule_matches(rule, tool, command):
            return (f"The {agent['role']} does not hold `{capability}`, which `{rule}` belongs to ({where}). "
                    f"Report what you would do, and the Lead does it or asks the owner.")
    return None


def deny(reason: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}})
