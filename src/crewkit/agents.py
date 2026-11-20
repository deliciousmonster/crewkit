# Claude Code's side of a crew, shared by every crew's renderer: the model a tier names, the tools a role's
# capabilities bind, an agent file's frontmatter, and the role policy the crews' guard hooks read.
import json
import re


def kebab(name: str) -> str:
    return name.replace("_", "-")


def model(models: dict, tier: str) -> str:
    provider = models["runtimes"]["claude_code"]["provider"]
    found = (models["providers"].get(provider) or {}).get(tier)
    if not found:
        raise ValueError(f"models.yaml: no {tier} model for {provider}")
    return found


def capabilities(tools: dict, role: str) -> list:
    """(name, capability) for each capability in tools.yaml the role holds, in file order."""
    return [(n, cap) for n, cap in tools["capabilities"].items() if role in cap.get("roles", [])]


def tool_names(tools: dict, role: str) -> list:
    return list(dict.fromkeys(t for _, cap in capabilities(tools, role) for t in (cap.get("claude_code") or {}).get("tools") or []))


def header(name: str, description: str, tools: list | None, model_id: str) -> str:
    """An agent file's frontmatter. tools None leaves the allowlist out: the subagent inherits the session's
    tools and the guard holds it to the policy's `tools`, which a session reads fresh on every call."""
    return "\n".join(["---", f"name: {name}", f"description: {json.dumps(description, ensure_ascii=False)}",
                      *([f"tools: {', '.join(tools)}"] if tools is not None else []), f"model: {model_id}", "---"])


def _rules(cap: dict, consequential_only: bool) -> list:
    cc = cap.get("claude_code") or {}
    return [r for k in ("allow", "ask") for r in cc.get(k) or [] if not consequential_only or k == "ask" or cap.get("outbound")]


def policy(tools: dict, agents: dict, workspace: str, never: list, gated: str | None = None,
           parallel: dict | None = None) -> dict:
    """The guard's policy: for each agent name (mapped to its role), the rules of capabilities the role does
    not hold that are consequential (listed under `ask`, or in an outbound capability), less any rule a held
    capability also grants, its tools, whether the role holds the `gated` capability, and how many copies of
    itself it may run at once (`parallel`, role to a count; absent or 0, none); every bound tool; the never-list."""
    out = {}
    for name, role in agents.items():
        held = {r for _, cap in capabilities(tools, role) for r in _rules(cap, False)}
        deny = {r: n for n, cap in tools["capabilities"].items() if role not in cap.get("roles", [])
                for r in _rules(cap, True) if r not in held}
        out[name] = {"role": role, "deny": deny, "tools": tool_names(tools, role),
                     "parallel": int((parallel or {}).get(role) or 0),
                     "gated": bool(gated) and any(n == gated for n, _ in capabilities(tools, role))}
    bound = sorted({t for cap in tools["capabilities"].values() for t in (cap.get("claude_code") or {}).get("tools") or []})
    return {"workspace": workspace, "never": list(never), "bound": bound, "agents": out}


# A background agent's notifications carry the description it was spawned with, and Claude Code keeps that label
# for the agent's life: a SendMessage that continues it under a new target changes neither the label nor anything
# crewkit can reach. On 2026-10-29 an agent spawned as "Apply: Netflix Agent Platform SEM" was continued for
# Asurion and Parallax, and every notification still read Netflix. What a crew can own is below: a label built the
# same way every time, a Lead rule of one agent per target, and a first line in every report that names the target,
# so the notification's body reads right even where its title cannot.

def label(flow: str, target: str, width: int = 60) -> str:
    """The Agent tool's description for one flow on one target, `Apply: Asurion - Staff SEM`: one line, at
    most `width` characters, cut at a word where it must be cut."""
    text = re.sub(r"\s+", " ", f"{flow.strip()}: {target.strip()}").strip()
    if len(text) <= width:
        return text
    cut = text[: width - 3].rsplit(" ", 1)[0]
    return (cut if len(cut) > len(flow) + 1 else text[: width - 3]).rstrip(" :,-") + "..."


TARGET_LINE = "Target:"


def target_of(report: str) -> str | None:
    """The target a report's first non-blank line names (`Target: Asurion - Staff SEM`), or None."""
    first = next((line.strip() for line in report.splitlines() if line.strip()), "")
    return (first[len(TARGET_LINE):].strip() or None) if first.startswith(TARGET_LINE) else None

LEAD_DELEGATION = """\
- One agent per target. Spawn a fresh agent for each posting, title or repository a flow works on, with the
  description `<flow>: <target>` (crewkit.agents.label builds it), and run several at once by spawning several.
  Claude Code labels every notification from an agent with the description it was spawned with and never
  relabels it, so an agent continued for a second target reports under the first one's name.
- Continue an agent with SendMessage only for the target it was spawned for: to relay the owner's answer to
  the question it returned, or to have it read back what you clicked. Open the message with
  `Target: <target>` and set `summary` to the same, so your own transcript row names it too.
- Every subagent's report opens with `Target: <target>`. When a notification's title and that line disagree,
  the line is the truth; say which target in your reply to the owner, never the title alone."""

SUBAGENT_TARGET = """\
- Open every report, the first and each one after a continuation, with one line `Target: <target>`: the
  posting, title or repository this report is about, as the Lead named it. The Lead reads that line, not the
  notification's title, which Claude Code fixed when you were spawned. If a continuation names a different
  target than the one you were spawned for, do the work, and say on the second line that you were spawned for
  <the first target>, so the Lead spawns a fresh agent next time."""
