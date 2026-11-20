"""What a crew declares in its [tool.crewkit] table, where its installed files go, and `doctor`'s check of the box.

Standard library only, so the installer runs without the `run` extra.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MIN_PYTHON = (3, 11)


class InstallError(Exception):
    """A failure with a message fit to print as it is."""


@dataclass
class Prereq:
    name: str
    level: str = "soft"  # "hard" fails doctor; "soft" only warns
    binaries: list[str] = field(default_factory=list)  # any one on PATH satisfies it; empty is a manual note
    hint: str = ""


@dataclass
class Spec:
    root: Path
    name: str
    skills: list[str]
    skills_dir: str = "skills"
    agents_dir: str | None = None
    hook: str | None = None  # SessionStart command template; {crew_path} is the crew root
    hooks: list[dict] = field(default_factory=list)  # [[tool.crewkit.hooks]]: event, command, optional matcher
    permissions: list[str] = field(default_factory=list)  # settings.json permissions.allow entries
    repo: str = ""  # "owner/name", named when access to GitHub is missing
    bin: list[str] = field(default_factory=list)  # console scripts given a launcher in bin_dir()
    prerequisites: list[Prereq] = field(default_factory=list)
    flows: dict = field(default_factory=dict)  # [tool.crewkit.flows]: tools, crew, path, for `crewkit lint`

    @property
    def command(self) -> str:
        """What the owner types to reach this crew's installer, so a hint names a command that exists."""
        return self.bin[0] if self.bin else f"uv run --directory {shlex.quote(str(self.root))} crewkit"


def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def bin_dir() -> Path:
    return Path(os.environ.get("CREWKIT_BIN") or home() / ".local" / "bin")


def config_dir(crew: str) -> Path:
    return home() / ".config" / crew


def manifests() -> list[tuple[Path, object]]:
    """Each installed crew's ~/.config/<crew>/install.json and what it parses to, None when it does not."""
    out: list[tuple[Path, object]] = []
    for path in sorted((home() / ".config").glob("*/install.json")):
        try:
            out.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            out.append((path, None))
    return out


def installed_skills() -> set[str]:
    """The skill names every installed crew linked, at user or project scope; unreadable records are skipped."""
    out = set()
    for _, man in manifests():
        links = man.get("links") if isinstance(man, dict) else None
        for link in links if isinstance(links, list) else []:
            p = link.get("path") if isinstance(link, dict) else None
            if isinstance(p, str) and Path(p).parent.name == "skills":
                out.add(Path(p).name)
    return out


def _stopline_row(spec: Spec, row) -> None:
    """Whether the interpreter a declared crewkit.stopline hook names can import it; the hook itself never says."""
    if not any("crewkit.stopline" in h.get("command", "") for h in spec.hooks):
        return
    py = spec.root / ".venv" / "bin" / "python"
    try:
        r = subprocess.run([str(py), "-I", "-m", "crewkit.stopline", "--check"], capture_output=True, text=True,
                           timeout=5, check=False)
        ok = r.stdout.strip() == "stopline ok"
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    row(ok, "soft", "stopline hook", f"run `uv sync` in {shlex.quote(str(spec.root))}")


def load_spec(root: Path) -> Spec:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise InstallError(f"{root} has no pyproject.toml; pass the crew folder with --root")
    data = tomllib.loads(pyproject.read_text())
    table = data.get("tool", {}).get("crewkit")
    if table is None:
        raise InstallError(f"{pyproject} has no [tool.crewkit] table")
    name = table.get("name") or data.get("project", {}).get("name")
    skills = table.get("skills")
    if not name or not skills or not all(isinstance(s, str) for s in skills):
        raise InstallError(f"{pyproject}: [tool.crewkit] needs `skills = [\"<dir>\", ...]` (and a project name)")
    unknown = set(table) - {"name", "skills", "skills_dir", "agents_dir", "hook", "hooks", "permissions", "repo",
                            "prerequisites", "bin", "flows"}
    if unknown:
        raise InstallError(f"{pyproject}: unknown [tool.crewkit] keys: {', '.join(sorted(unknown))}")
    prereqs = []
    for p in table.get("prerequisites", []):
        if p.get("level", "soft") not in ("hard", "soft") or "name" not in p:
            raise InstallError(f"{pyproject}: a prerequisite needs a name and level hard or soft: {p}")
        prereqs.append(Prereq(p["name"], p.get("level", "soft"), list(p.get("binaries", [])), p.get("hint", "")))
    hooks = []
    for h in table.get("hooks", []):
        if not isinstance(h.get("event"), str) or not isinstance(h.get("command"), str) or set(h) - {"event", "command", "matcher"}:
            raise InstallError(f"{pyproject}: a hook needs an event and a command, and may have a matcher: {h}")
        hooks.append(dict(h))
    flows = table.get("flows", {})
    if flows and (not isinstance(flows, dict) or set(flows) != {"tools", "crew", "path"}
                  or not all(isinstance(v, str) for v in flows.values()) or "{flow}" not in flows["path"]):
        raise InstallError(f"{pyproject}: [tool.crewkit.flows] needs tools, crew and path (with {{flow}} in it): {flows}")
    return Spec(root.resolve(), name, list(skills), table.get("skills_dir", "skills"), table.get("agents_dir"),
                table.get("hook"), hooks, list(table.get("permissions", [])), table.get("repo", ""), prerequisites=prereqs,
                bin=list(table.get("bin", [])), flows=dict(flows))


def find_root(start: Path) -> Path:
    """The nearest folder at or above `start` whose pyproject.toml has a [tool.crewkit] table."""
    for d in [start.resolve(), *start.resolve().parents]:
        f = d / "pyproject.toml"
        if f.is_file():
            try:
                if "crewkit" in tomllib.loads(f.read_text()).get("tool", {}):
                    return d
            except tomllib.TOMLDecodeError:
                continue
    raise InstallError(f"no [tool.crewkit] pyproject.toml at or above {start}; run from the crew clone or pass --root")


def resolve_crew_path(crew: str, skill_dir: Path | None = None, env: dict | None = None) -> Path | None:
    """What a SKILL.md does, in order: $<CREW>_HOME, ~/.config/<crew>/crew-path, then two levels up
    from the real path of the skill's own directory (<crew>/skills/<name>)."""
    env = os.environ if env is None else env
    var = crew.upper().replace("-", "_") + "_HOME"
    if env.get(var) and Path(env[var]).is_dir():
        return Path(env[var]).resolve()
    f = config_dir(crew) / "crew-path"
    if f.is_file():
        p = Path(f.read_text().strip())
        if p.is_dir():
            return p.resolve()
    if skill_dir is not None:
        p = skill_dir.resolve().parents[1]
        if p.is_dir():
            return p
    return None


def doctor(spec: Spec | None = None, which=None) -> tuple[int, list[str]]:
    """(exit code, report lines). Non-zero only when a hard prerequisite is missing."""
    which = which or shutil.which
    lines, hard_miss = [], False

    def row(ok: bool, level: str, what: str, hint: str = ""):
        nonlocal hard_miss
        mark = "ok  " if ok else ("FAIL" if level == "hard" else "warn")
        lines.append(f"[{mark}] {what}" + ("" if ok or not hint else f" - {hint}"))
        hard_miss |= not ok and level == "hard"

    for tool, hint in (("uv", "install from https://docs.astral.sh/uv/"), ("git", "install git")):
        row(bool(which(tool)), "hard", tool, hint)
    py = sys.version_info[:2]
    row(py >= MIN_PYTHON, "hard", f"python {py[0]}.{py[1]}", f"needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer")
    if spec and spec.bin and str(bin_dir()) not in os.environ.get("PATH", "").split(os.pathsep):
        row(False, "soft", f"{bin_dir()} on PATH",
            f"add it to PATH, or run `uv run --project {shlex.quote(str(spec.root))} <command>`")
    if sys.platform == "win32":
        row(False, "hard", "platform", "Windows is unsupported; use macOS or Linux")
    for p in (spec.prerequisites if spec else []):
        if p.binaries:
            found = next((b for b in p.binaries if which(b)), None)
            row(bool(found), p.level, f"{p.name} ({'/'.join(p.binaries)})", p.hint)
        else:
            lines.append(f"[note] {p.name}" + (f" - {p.hint}" if p.hint else ""))
    if spec:
        _stopline_row(spec, row)
    return (1 if hard_miss else 0), lines
