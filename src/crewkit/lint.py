# Checks a crew's tests run over its skill text, so an agent following it never builds a command it cannot run.
import re
from pathlib import Path

ALIAS = [
    (re.compile(r"`[\w.-]+` below is `"), "names a command alias; write the command out each time"),
    (re.compile(r"^\s*[A-Za-z_]\w*=([\"'])[^\"']*\s[^\"']*\1\s*$"), "keeps a multi-word command in a variable"),
    (re.compile(r"(?:^|[;&|(]\s*)\"?\$\{?[A-Za-z_]\w*\}?\"?\s"), "runs a variable as a command, which zsh does not split"),
]


def aliases(text: str) -> list:
    """(line number, line, why) for each line that sets up a command as an alias or runs one from a variable."""
    return [(n, line.strip(), why) for n, line in enumerate(text.splitlines(), 1)
            for pattern, why in ALIAS if pattern.search(line)]


def check_skills(root) -> list:
    """aliases() over every .md under root, as `path:line: why: text`."""
    return [f"{p}:{n}: {why}: {line}" for p in sorted(Path(root).rglob("*.md"))
            for n, line, why in aliases(p.read_text())]


# A flow declares the tools it asks of the role that runs it on a `**Tools:**` line, each name in backticks:
#
#     **Tools:** `Read`, `Grep`, `Bash`
#     **Tools (lead):** `WebSearch`, `mcp__claude-in-chrome__navigate`
#     **Tools:** none.
#
# An unscoped line binds every role that runs the flow; `**Tools (<role>, ...):**` binds only the roles it names,
# for the steps a flow gives one of them (the Lead's steps in a flow a subagent also follows). A declaration runs
# on until a blank line, a list item, a heading or the next `**...:**` line. Prose is never read for tools.
TOOLS_LINE = re.compile(r"^\*\*Tools(?:\s*\(([^)]*)\))?:\*\*(.*)$")
STOP = re.compile(r"^\s*$|^\s*(?:[-*+]|\d+\.)\s|^#|^\*\*[^*]+:\*\*")


def flow_tools(text: str) -> tuple[dict, list]:
    """({scope: [tool, ...]}, problems) for a flow's `**Tools:**` declarations. The scope is None for an
    unscoped line, or a role's name; a problem is (line number, why). No declaration gives ({}, [])."""
    lines, found, problems = text.splitlines(), {}, []
    for i, line in enumerate(lines):
        m = TOOLS_LINE.match(line.strip())
        if not m:
            continue
        body = [m.group(2)]
        for more in lines[i + 1:]:
            if STOP.match(more):
                break
            body.append(more)
        body = " ".join(body)
        names = re.findall(r"`([^`]+)`", body)
        if not names and not re.match(r"\s*none\b", body, re.I):
            problems.append((i + 1, "a **Tools:** line names no tool in backticks and does not say none"))
        scopes = [s.strip() for s in m.group(1).split(",") if s.strip()] if m.group(1) is not None else [None]
        for scope in scopes:
            found.setdefault(scope, [])
            found[scope] += [n.strip() for n in names if n.strip() not in found[scope]]
    return found, problems


def check_flow_tools(tools: dict, flows: dict, roles: list | None = None) -> list:
    """Each tool a role's flows declare that the role does not hold in tools.yaml, as
    `<role>: <flow file>: <tool> is not among the role's tools (...)`, plus each flow that is missing, declares
    nothing, or scopes a line to a role not in `roles`. `flows` maps a role to its flow files (paths); `roles`
    is every role the crew has (default: the roles in `flows`). Empty when every flow is covered."""
    from crewkit.agents import tool_names
    from crewkit.guard import held

    known = set(roles if roles is not None else flows)
    out = []
    for role, paths in flows.items():
        holds = tool_names(tools, role)
        for path in map(Path, paths):
            if not path.is_file():
                out.append(f"{role}: {path}: flow file is missing")
                continue
            found, problems = flow_tools(path.read_text())
            out += [f"{role}: {path}:{n}: {why}" for n, why in problems]
            if not found:
                out.append(f"{role}: {path}: declares no tools; add a **Tools:** line (or **Tools:** none.)")
                continue
            out += [f"{role}: {path}: **Tools ({s}):** names a role the crew does not have"
                    for s in found if s is not None and s not in known]
            asked = found.get(None, []) + [t for t in found.get(role, []) if t not in found.get(None, [])]
            out += [f"{role}: {path}: {t} is not among the role's tools ({', '.join(holds) or 'none'})"
                    for t in asked if not held(t, holds)]
    return out


def flows_of(crew: dict, path: str, root=".") -> dict:
    """{role: [flow file, ...]} from crew.yaml's `roles.<role>.claude_code.flows`, each name placed into `path`
    (`skills/other/flows/{flow}.md`) under root."""
    cc = {role: (spec or {}).get("claude_code") or {} for role, spec in (crew.get("roles") or {}).items()}
    return {role: [Path(root) / path.format(flow=f) for f in c["flows"]] for role, c in cc.items() if c.get("flows")}
