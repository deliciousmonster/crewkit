# A crew's workspace: <workdir>/agents/<crew>/, or <workdir>/agents/ for a folder's only crew, holding one record copied
# from the crew's templates/workspace/ and kept by whatever versions <workdir> (git, a synced drive, nothing).
import re
import subprocess
from pathlib import Path

import yaml

SYNCED = re.compile(r"/(Google Drive|GoogleDrive|CloudStorage|Mobile Documents|Dropbox|OneDrive)(/|$)")
SKIP = {".git", "node_modules", "target", "dist", "build", ".venv", "worktrees", ".wt", "vendor"}


def git(folder: Path, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def vcs_of(folder: Path) -> str:
    """How a folder is versioned: git, drive (a cloud-synced folder) or none."""
    if git(folder, "rev-parse", "--show-toplevel") is not None:
        return "git"
    return "drive" if SYNCED.search(str(Path(folder).resolve())) else "none"


def workdir_of(path: str | Path | None, dirs: list[str], marker: str | None = None) -> Path:
    """The working directory a path names; a path inside one of `dirs` (agents/<crew>, or agents) means the folder
    that holds it. With `marker`, a file relative to the workspace, a folder counts only when it holds one, so a
    project's own agents/ code is never taken for a record."""
    p = Path(path).expanduser().resolve() if path else Path.cwd()
    p = p.parent if p.is_file() else p
    for d in [p, *p.parents]:
        for ws in dirs:
            if str(d).endswith("/" + ws) and (marker is None or (d / marker).is_file()):
                return Path(str(d)[: -len(ws) - 1])
    if not p.is_dir():
        raise NotADirectoryError(p)
    return p


def skeleton(template: Path, vcs: str) -> dict[str, str]:
    """The template's files by relative path, with {{vcs}} filled in."""
    return {str(f.relative_to(template)): f.read_text().replace("{{vcs}}", vcs) for f in sorted(template.rglob("*")) if f.is_file()}


def init(workdir: Path, ws: Path, template: Path, *, sdlc: bool = False) -> list[str]:
    """Copy the template into `ws`, never overwriting; with `sdlc`, then the SDLC deliverables the crew's template
    lacks. Returns the files written."""
    written = []
    for rel, text in skeleton(template, vcs_of(workdir)).items():
        dst = ws / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text)
            written.append(rel)
    if sdlc:
        from crewkit.sdlc import scaffold
        written += scaffold(ws)
    return written


def read_yaml(path: Path) -> dict:
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def policy(workdir: Path, ws: Path) -> dict:
    """workspace.yaml's vcs block over what the folder shows: kind, commit, push, decided."""
    return {"kind": vcs_of(workdir), "commit": "ask", "push": "ask", "decided": None, **(read_yaml(ws / "workspace.yaml").get("vcs") or {})}


def _looks_like_record(names: set[str]) -> bool:
    return "state.md" in names or ("AGENTS.md" in names and bool(names & {"journal", "state", "INTAKE.md"})) or {"knowledge", "journal"} <= names


def find_records(workdir: Path, exclude: str, depth: int = 3) -> list[str]:
    """Folders under `workdir` that hold agent records in some older layout, for the owner to pick from."""
    found = []

    def visit(d: Path, level: int):
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        rel = str(d.relative_to(workdir))
        if d != workdir and (_looks_like_record({e.name for e in entries}) or re.search(r"(^|/)agents/[^/]+$", rel)):
            found.append(rel)
        elif level < depth:
            for e in sorted(entries):
                if e.is_dir() and not e.is_symlink() and e.name not in SKIP:
                    visit(e, level + 1)

    visit(Path(workdir), 0)
    return [r for r in found if r != exclude]
