"""Builds a crew for any run of tasks from a CrewConfig.

One agent per (role, tier), the manager from crew.yaml, each role's knowledge inlined into its backstory, and the
tools tools.yaml binds for it. A task marked `runtime: claude_code` needs a signed-in browser or the owner's approval
and is skipped here; a task's `gate` names a guardrail factory in the crew's registry. CrewAI is the optional
`crewkit[run]` extra, so it is imported when a crew is built, not when this module loads.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from crewkit.config import CrewConfig
from crewkit.llm import llm

if TYPE_CHECKING:
    from crewai import Agent, Crew


def built_in() -> dict:
    """The CrewAI tools every crew may bind. Custom tools join the registry as each crew implements
    them; an unbuilt binding is skipped."""
    from crewai_tools import DirectoryReadTool, FileReadTool, FileWriterTool, ScrapeWebsiteTool

    return {c.__name__: c for c in (DirectoryReadTool, FileReadTool, FileWriterTool, ScrapeWebsiteTool)}


# Keys this layout adds to CrewAI's agent and task schemas, removed before CrewAI sees them.
AGENT_EXTRAS = ("tier",)
TASK_EXTRAS = ("agent", "tier", "context", "track", "runtime", "gate")


def build_agent(cfg: CrewConfig, role: str, inputs: dict, tier: str | None = None, registry=None) -> Agent:
    registry = {**built_in(), **(registry or {})}
    config = {k: v for k, v in cfg.agents[role].items() if k not in AGENT_EXTRAS}
    # Knowledge goes in as an input, since CrewAI reads a bare {name} in a backstory as a placeholder.
    inputs[f"knowledge_{role}"] = cfg.knowledge(role, inputs)
    config["backstory"] = config.get("backstory", "") + "\n\n{knowledge_" + role + "}"
    # CrewAI refuses a manager that holds tools.
    # Two capabilities may bind one class (a runner and a narrower use of it); the agent gets it once.
    classes = [] if role == cfg.manager else dict.fromkeys(b["class"] for b in cfg.tool_bindings(role) if b["class"] in registry)
    tools = [registry[name]() for name in classes]
    from crewai import Agent

    return Agent(config=config, llm=llm(cfg.models, tier or cfg.agents[role]["tier"], cfg.key_file), tools=tools)


def build_crew(cfg: CrewConfig, names: list[str], inputs: dict, registry=None, gates=None) -> Crew:
    from crewai import Crew, Process, Task

    if unknown := [n for n in names if n not in cfg.tasks]:
        raise SystemExit(f"unknown tasks: {', '.join(unknown)}")
    skipped = [n for n in names if cfg.tasks[n].get("runtime") == "claude_code"]
    for name in skipped:
        print(f"skipped {name}: runs under Claude Code only", file=sys.stderr)
    names = [n for n in names if n not in skipped]
    if not names:
        raise SystemExit("every task named runs under Claude Code only")
    workers, built = {}, {}
    for name in names:
        spec = cfg.tasks[name]
        role, tier = spec.get("agent"), spec.get("tier")
        worker = None
        if role:
            key = (role, tier or cfg.agents[role]["tier"])
            if key not in workers:
                workers[key] = build_agent(cfg, role, inputs, key[1], registry)
            worker = workers[key]
        # A task whose named context is not in this run reads every earlier output of the run instead,
        # CrewAI's default, so a chain such as baseline, build_change, break_change hands work along.
        context = [built[n] for n in spec.get("context", []) if n in built]
        config = {k: v for k, v in spec.items() if k not in TASK_EXTRAS}
        extra = {"context": context} if context or not built else {}
        if spec.get("gate"):
            if spec["gate"] not in (gates or {}):
                raise SystemExit(f"task {name}: no gate {spec['gate']} registered")
            extra["guardrail"] = gates[spec["gate"]](inputs)
        built[name] = Task(config=config, agent=worker, name=name, **extra)
    hierarchical = cfg.crew.get("process") == "hierarchical"
    crew = Crew(
        agents=list(workers.values()),
        tasks=list(built.values()),
        process=Process.hierarchical if hierarchical else Process.sequential,
        manager_agent=build_agent(cfg, cfg.manager, inputs, registry=registry) if hierarchical else None,
    )
    if hierarchical:
        # Kickoff fills placeholders in crew.agents only, so the manager kept {request} and its knowledge
        # input literal in every call on 2026-10-15 until it is filled here.
        crew.manager_agent.interpolate_inputs(inputs)
        # The manager runs each task with the tools CrewAI copied onto it, so the Lead did the Scout's work on
        # 2026-10-15. Validation refills the field, so it is cleared on the built crew; delegation copies anew.
        for task in crew.tasks:
            object.__setattr__(task, "tools", [])
    return crew

def describe(crew: Crew) -> str:
    lines = []
    if crew.manager_agent:
        lines.append(f"manager {crew.manager_agent.role.strip()} on {crew.manager_agent.llm.model}")
    for a in crew.agents:
        lines.append(f"agent {a.role.strip()} on {a.llm.model}, tools {[t.name for t in a.tools]}")
    for t in crew.tasks:
        lines.append(f"task {t.name} -> {t.agent.role.strip() if t.agent else 'manager'}")
    return "\n".join(lines)
