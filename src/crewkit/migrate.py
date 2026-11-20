# Gather a folder's agent files into its workspace: relocate (or, inside it, lift) a legacy folder, aggregate records
# kept elsewhere, add what the template lacks, organize dated evidence, format knowledge, rewrite old paths. Commits nothing.
import fnmatch
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from crewkit.workspace import git, skeleton, vcs_of

REFERENCE_FILES = [".claude/settings.json", ".claude/settings.local.json", ".git/info/exclude", "CLAUDE.md", "AGENTS.md"]
DATED = re.compile(r"(\d{4}-\d{2}-\d{2})")
DROP = re.compile(r"(^|/)node_modules/.*$|^CLAUDE\.md$")


@dataclass
class Layout:
    ws: str  # the workspace, relative to the folder: agents/<crew>
    legacy: str | None  # where it lived before, read and relocated, never written
    template: Path
    old_paths: list[tuple[str, str]] = field(default_factory=list)  # regex -> replacement, for references
    moves: dict[str, str] = field(default_factory=dict)  # a lift: a legacy entry -> its path in ws, else its own name
    history: list[str] = field(default_factory=list)  # a lift: ws paths whose text keeps the old paths (journal, evidence)
    archive: str = "archive"  # a lift: where root files the layout has no place for, and path-moves.md, go
    marker: str | None = None  # a file in ws that only this layout has; with no legacy record left, migrate stops
    sdlc: bool = False  # scaffold the SDLC deliverables ws lacks; never moves content into them
    sdlc_sources: dict[str, str] = field(default_factory=dict)  # a glob under ws -> the deliverable it is a draft source for


def kebab(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", re.sub(r"\.md$", "", text)).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def normalize_knowledge(text: str, name: str) -> tuple[str, str, bool]:
    """A knowledge file in one frontmatter dialect: (name, text, whether it lacked a description)."""
    m = re.match(r"---\n([\s\S]*?)\n---\n?", text)
    fields, body = {}, text
    if m:
        body, section = text[m.end():], ""
        for line in m.group(1).split("\n"):
            if top := re.match(r"([\w-]+):\s*(.*)$", line):
                section = top.group(1)
                if top.group(2).strip():
                    fields[section] = top.group(2).strip()
            elif nested := re.match(r"\s+([\w-]+):\s*(.*)$", line):
                fields[f"{section}.{nested.group(1)}"] = nested.group(2).strip()
    unquote = lambda v: json.loads(v) if v and re.fullmatch(r'".*"', v) else v  # noqa: E731
    load_when = re.search(r"(?m)^Load when:\s*(.+)$", body)
    description = unquote(fields.get("description")) or (f"Load when: {load_when.group(1)}" if load_when else None)
    slug = kebab(unquote(fields.get("name")) or name)
    kind = fields.get("metadata.type") or fields.get("type") or fields.get("metadata.node_type") or "memory"
    kept = [f"  {k[9:]}: {v}" for k, v in fields.items() if k.startswith("metadata.") and k not in ("metadata.type", "metadata.node_type")]
    head = ["---", f"name: {slug}", f"description: {json.dumps(description or '')}", "metadata:", f"  type: {kind}", *kept, "---", ""]
    return slug, "\n".join(head) + "\n" + body.lstrip("\n"), not description


def _files(d: Path) -> list[str]:
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if (p.is_file() or p.is_symlink()) and ".git" not in p.relative_to(d).parts) if d.is_dir() else []


def _aggregate(ws: Path, sources: list[str], present, review: list) -> list[dict]:
    steps = []
    for spec in sources:
        src, _, into = spec.partition("=")
        src = Path(src).expanduser().resolve()
        if not src.is_dir():
            review.append(f"--from {spec}: no such folder")
            continue
        tag = kebab(src.name)
        for rel in _files(src):
            to = str(Path(into) / rel) if into else rel
            if DROP.search(rel):
                continue
            if not present(to):
                steps.append({"kind": "aggregate", "from": src / rel, "to": ws / to})
            elif re.fullmatch(r"journal/\d{4}-\d{2}-\d{2}\.md", rel):
                steps.append({"kind": "append", "from": src / rel, "to": ws / to, "note": f"From {tag}"})
            else:
                kept = (ws / to).with_name(f"{Path(to).stem}.from-{tag}{Path(to).suffix}")
                steps.append({"kind": "aggregate", "from": src / rel, "to": kept})
                review.append(f"{ws / to} collides with {src / rel}; the incoming copy is {kept}")
    return steps


def _organize(ws: Path, source: Path) -> list[dict]:
    steps = []
    for e in sorted(source.iterdir()) if source.is_dir() else []:
        dated = re.fullmatch(r"evidence-(\d{4}-\d{2}-\d{2})", e.name)
        if e.is_dir() and not e.is_symlink() and dated:  # cited paths keep resolving through a link at the old name
            steps.append({"kind": "organize", "from": ws / e.name, "to": ws / "evidence" / dated[1], "link": f"evidence/{dated[1]}"})
        elif e.is_file() and re.search(r"\.(log|txt|json|tsv|out)$", e.name) and e.name != "manifest.json":
            day = (DATED.search(e.name) or [None, date.fromtimestamp(e.stat().st_mtime).isoformat()])[1]
            steps.append({"kind": "organize", "from": ws / e.name, "to": ws / "evidence" / day / e.name})
    return steps


def same(a: Path, b: Path) -> bool:
    """Whether two existing paths are one file, as os.path.samefile says but on the links themselves: INDEX.md and
    index.md on a case-insensitive volume (APFS and NTFS by default). Writing one and unlinking the other loses it."""
    try:
        return os.path.samestat(os.lstat(a), os.lstat(b))
    except OSError:
        return False


def _format(ws: Path, source: Path, review: list) -> list[dict]:
    steps, targets = [], {}
    for f in sorted((source / "knowledge").glob("*.md")):
        if f.name == "README.md":
            continue
        slug, text, missing = normalize_knowledge(f.read_text(), f.name)
        to, there = ws / "knowledge" / f"{slug}.md", source / "knowledge" / f"{slug}.md"  # there: the target before a relocation
        if to.name != f.name and os.path.lexists(there) and not same(f, there):
            review.append(f"knowledge/{f.name} would replace knowledge/{to.name}; merge them by hand")
            continue
        if text != f.read_text() or to.name != f.name:
            steps.append({"kind": "format", "from": ws / "knowledge" / f.name, "to": to, "text": text})
        if missing:
            review.append(f"knowledge/{f.name} has no description; write one")
        if to in targets:
            review.append(f"knowledge/{f.name} and {targets[to]} both become {to}; merge them by hand")
        targets[to] = f"knowledge/{f.name}"
    return steps


def _references(folder: Path, layout: Layout, skip: list[Path]) -> list[dict]:
    tracked = (git(folder, "ls-files", "*.md") or "").splitlines()
    steps = []
    for rel in dict.fromkeys(REFERENCE_FILES + tracked):
        path = folder / rel
        if not path.is_file() or any(s in path.parents for s in skip):
            continue
        text = path.read_text()
        new = text
        for old, to in layout.old_paths:
            new = re.sub(old, to, new)
        if new != text:
            steps.append({"kind": "reference", "to": path, "text": new})
    return steps


MAP = re.compile(r"<!-- map[\s\S]*?<!-- /map -->\n?")
EDGE = r"(?<![\w./~-]){}(?![\w-]|\.[\w-])"  # a whole path: not another path's tail nor a longer name's start; a period ends it


def _within(rel: str, paths: list[str]) -> bool:
    return any(rel == h or rel.startswith(h.rstrip("/") + "/") for h in paths)


def _lift_moves(folder: Path, ws: Path, legacy: Path, layout: Layout, template: dict) -> tuple:
    """(lifts, folds, collisions, moved): each legacy entry to the place `moves` names, a root file the template has
    no place for to the archive, anything else under its own name, and each evidence-<date>/ into evidence/<date>/.
    A folder whose new place is inside another entry's (procedures/ into knowledge/procedures/, evidence-<date>/
    into evidence/) folds in file by file after the whole-folder lifts, so it never lands inside a folder git mv
    would then move it into."""
    how = lambda p: "git" if git(folder, "ls-files", "--", str(p.relative_to(folder))) else "rename"  # noqa: E731
    entries = sorted(p for p in legacy.iterdir() if p.name != ".DS_Store")
    dated = {e.name: m[1] for e in entries if (m := re.fullmatch(r"evidence-(\d{4}-\d{2}-\d{2})", e.name))}
    moved = {e.name: f"evidence/{dated[e.name]}" if e.name in dated else layout.moves.get(e.name) or (
        f"{layout.archive}/{e.name}" if e.is_file() and not e.name.startswith(".") and e.name not in template else e.name)
        for e in entries}
    movers = {e.name: moved[e.name] for e in entries if not e.is_symlink()}  # what brings content into ws

    def holder(rel: str, own: str) -> Path | None:
        """What already holds ws/rel, in ws or brought there by another entry's move, or None."""
        if os.path.lexists(ws / rel):
            return ws / rel
        return next((legacy / a / rel[len(b):].lstrip("/") for a, b in movers.items()
                     if a != own and _within(rel, [b]) and os.path.lexists(legacy / a / rel[len(b):].lstrip("/"))), None)

    lifts, folds, collisions = [], [], []
    for e in entries:
        to = moved[e.name]
        nested = any(a != e.name and b != to and _within(to, [b]) for a, b in movers.items())
        if e.name in dated and e.is_symlink():
            if e.resolve() == (legacy / to).resolve():  # the link an earlier migration left
                folds.append({"kind": "unlink", "from": e, "to": ws / to, "how": how(e)})
            else:
                collisions.append(f"{e} links to {os.readlink(e)}, not to {to}")
        elif e.is_dir() and not e.is_symlink() and (nested or e.name in dated):
            for rel in [r for r in _files(e) if Path(r).name != ".DS_Store"]:
                if held := holder(f"{to}/{rel}", e.name):
                    collisions.append(f"{e / rel} and {held}")
                else:
                    folds.append({"kind": "lift", "from": e / rel, "to": ws / to / rel, "how": how(e / rel)})
        elif held := holder(to, e.name):
            collisions.append(f"{e} and {held}")
        else:
            (folds if nested else lifts).append({"kind": "lift", "from": e, "to": ws / to, "how": how(e)})
    return lifts, folds, collisions, moved


def _lift_text(ws: Path, lifts: list, subs: list, layout: Layout, template: dict, moved: dict) -> list:
    """A reference step for each lifted text file outside `history` whose old paths change; a renamed root file the
    template holds (README.md as AGENTS.md) keeps its prose, names the record's files from its root, and gains the map."""
    rewrites = []
    for s in lifts:
        src = s["from"]
        for f in [src] if src.is_file() else [Path(r, n) for r, _, names in os.walk(src) for n in names]:
            rel = str(s["to"].relative_to(ws) / f.relative_to(src))
            if f.is_symlink() or _within(rel, layout.history):
                continue
            try:
                text = new = f.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for a, b in subs:
                new = re.sub(a, b, new)
            if f == src and src.name != Path(rel).name and rel in template:
                new = MAP.sub("", new)
                for a, b in sorted(moved.items(), key=lambda m: -len(m[0])):
                    new = new.replace(f"`{a}`", f"`{b}`")
                if m := MAP.search(template[rel]):
                    new = new.rstrip("\n") + "\n\n" + m.group(0)
            if new != text:
                rewrites.append({"kind": "reference", "from": ws / rel, "to": ws / rel, "text": new})
    return rewrites


def _lift(folder: Path, ws: Path, legacy: Path, layout: Layout) -> dict:
    """Move a legacy record inside the workspace up into it, overwriting nothing: each collision is listed and apply
    refuses the plan. The record's text then names the new paths (a moved file, or anything under the old folder's
    path with its slash), except under `history`, and so do the files outside it that migrate rewrites;
    <archive>/path-moves.md maps each old path to its new one."""
    template = skeleton(layout.template, vcs_of(folder))
    lifts, folds, collisions, moved = _lift_moves(folder, ws, legacy, layout, template)
    record = f"{layout.archive}/path-moves.md"
    if os.path.lexists(ws / record):
        collisions.append(f"{ws / record} exists")
    old, new = str(legacy.relative_to(folder)), str(ws.relative_to(folder))
    subs = [(EDGE.format(re.escape(f"{old}/{a}")), f"{new}/{b}") for a, b in sorted(moved.items(), key=lambda m: -len(m[0])) if a != b]
    subs.append((r"(?<![\w.-])" + re.escape(f"{old}/"), f"{new}/"))  # a folder path; a bare name may be a quoted ruling

    def present(rel: str) -> bool:
        return os.path.lexists(ws / rel) or any(
            os.path.lexists(legacy / a / rel[len(b):].lstrip("/")) for a, b in moved.items() if _within(rel, [b]))

    table = "\n".join([
        f"# Path moves, {date.today().isoformat()}", "",
        f"The record moved up from `{old}/` into `{new}/`. Each old path (a folder covers what it held) and where it is",
        "now; journal/ and evidence/ name the old paths as they were written.", "",
        "| Old path | New path |", "|---|---|", *(f"| `{old}/{a}` | `{new}/{b}` |" for a, b in moved.items()), ""])
    moving = [*lifts, *(f for f in folds if f["kind"] == "lift")]
    steps = [*lifts, *folds, *_lift_text(ws, moving, subs, layout, template, moved),
             *({"kind": "add", "to": ws / rel, "text": t} for rel, t in template.items() if not present(rel)),
             {"kind": "add", "to": ws / record, "text": table},
             *_references(folder, Layout(layout.ws, None, layout.template, [*layout.old_paths, *subs]), [ws]),
             {"kind": "remove", "from": legacy, "to": legacy}]
    return {"folder": folder, "ws": ws, "legacy": legacy, "vcs": vcs_of(folder), "steps": steps,
            "review": list(collisions), "collisions": collisions}


def _sdlc(planned: dict, layout: Layout) -> dict:
    """Add a scaffold step for each missing SDLC deliverable, and a review line for each draft source found."""
    from crewkit.sdlc import DELIVERABLES, md_files, template_text

    ws, legacy = planned["ws"], planned.get("legacy") or (planned["folder"] / layout.legacy if layout.legacy else None)
    missing = [rel for rel in DELIVERABLES if not os.path.lexists(ws / rel) and not (legacy and os.path.lexists(legacy / rel))]
    if not missing:
        return planned
    planned["steps"] = [*planned["steps"], *({"kind": "scaffold", "to": ws / rel, "text": template_text(rel)} for rel in missing)]
    planned["review"] = [r for r in planned["review"] if "nothing to do" not in r]
    found = list(md_files(ws)) if layout.sdlc_sources else []
    for pattern, into in layout.sdlc_sources.items():
        for f in found:
            rel = str(f.relative_to(ws))
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern.removeprefix("**/")):
                planned["review"].append(f"{rel}: draft source for {into}; `sdlc retire {rel}` once drafted")
    return planned


def plan(folder: Path, layout: Layout, sources: list[str] = ()) -> dict:
    planned = _plan(folder, layout, sources)
    return _sdlc(planned, layout) if layout.sdlc else planned


def _plan(folder: Path, layout: Layout, sources: list[str] = ()) -> dict:
    folder = Path(folder).resolve()
    ws, legacy = folder / layout.ws, folder / layout.legacy if layout.legacy else None
    if legacy and legacy.is_dir() and not legacy.is_symlink() and ws in legacy.parents:
        return _lift(folder, ws, legacy, layout)
    steps, review = [], []
    if layout.marker and (ws / layout.marker).is_file() and not (legacy and legacy.is_dir() and not legacy.is_symlink()):
        # Already in this layout: a rerun after the lift or relocation changes nothing; --from still gathers.
        steps = _aggregate(ws, list(sources), lambda rel: os.path.lexists(ws / rel), review)
        review += [] if sources else [f"{ws.relative_to(folder)}/ is already in this layout ({layout.marker}); nothing to do"]
        return {"folder": folder, "ws": ws, "vcs": vcs_of(folder), "steps": steps, "review": review, "migrated": True}
    if legacy and legacy.exists() and not legacy.is_symlink():
        if ws.exists():
            review.append(f"{legacy} and {ws} both exist; merge the legacy folder with --from {legacy}")
        else:  # a worktree moves with `git worktree move`, which keeps its link to the repository
            how = "worktree" if (legacy / ".git").exists() else "git" if git(folder, "ls-files", "--error-unmatch", str(legacy.relative_to(folder) / "state.md")) else "rename"
            steps.append({"kind": "relocate", "from": legacy, "to": ws, "how": how, "link": ws.name})
    source = legacy if steps else ws
    present = lambda rel: (ws / rel).exists() or (source / rel).exists()  # noqa: E731
    steps += _aggregate(ws, list(sources), present, review)
    steps += [{"kind": "add", "to": ws / rel, "text": t} for rel, t in skeleton(layout.template, vcs_of(folder)).items() if not present(rel)]
    steps += _organize(ws, source) + _format(ws, source, review) + _references(folder, layout, [p for p in (ws, legacy) if p])
    return {"folder": folder, "ws": ws, "vcs": vcs_of(folder), "steps": steps, "review": review}


def _rename_case(folder: Path, src: Path, dst: Path, tracked: bool):
    """Rename a file through a free name beside it, so neither name is ever written over the other even where the
    two are one file; git mv for a tracked file, so the index follows on a case-sensitive volume as well."""
    free = next(p for n in range(1000) if not os.path.lexists(p := src.with_name(f".{src.name}.case-{n}")))
    for a, b in ((src, free), (free, dst)):
        if tracked:
            subprocess.run(["git", "-C", str(folder), "mv", str(a.relative_to(folder)), str(b.relative_to(folder))], check=True)
        else:
            a.rename(b)


def _tracked(folder: Path, path: Path) -> bool:
    return bool(git(folder, "ls-files", "--", str(path.relative_to(folder))))


def _move(folder: Path, step: dict):
    src, dst = step["from"], step["to"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    if step["kind"] != "unlink" and same(src, dst):  # one file under two spellings: rename it, never move onto it
        if src.name != dst.name:
            _rename_case(folder, src, dst, step.get("how") == "git")
        return
    if step["kind"] == "unlink" and step["how"] == "git":
        subprocess.run(["git", "-C", str(folder), "rm", "-q", "--", str(src.relative_to(folder))], check=True)
    elif step["kind"] == "unlink":
        src.unlink()
    elif step.get("how") in ("worktree", "git"):
        verb = ["worktree", "move"] if step["how"] == "worktree" else ["mv"]
        subprocess.run(["git", "-C", str(folder), *verb, str(src.relative_to(folder)), str(dst.relative_to(folder))], check=True)
    elif dst.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        shutil.rmtree(src)
    else:
        src.rename(dst)
    if step.get("link"):
        src.symlink_to(step["link"])


class Refused(Exception):
    """A plan that would overwrite something; nothing was changed."""


def apply(planned: dict) -> list[str]:
    """Carry out the plan in order; the relocation runs first, so later paths are under the workspace."""
    if planned.get("collisions"):
        raise Refused("refused, nothing moved; these collide:\n" + "\n".join(f"  {c}" for c in planned["collisions"]))
    done, folder = [], planned["folder"]
    for s in planned["steps"]:
        kind, dst = s["kind"], s["to"]
        if kind in ("relocate", "organize", "lift", "unlink"):
            _move(folder, s)
        elif kind == "remove":  # what the moves left: empty folders and Finder's .DS_Store
            for d, _, _ in os.walk(dst, topdown=False):
                (Path(d) / ".DS_Store").unlink(missing_ok=True)
                Path(d).rmdir()
        elif kind == "aggregate":
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s["from"], dst, follow_symlinks=False)
        elif kind == "append":
            with dst.open("a") as f:
                f.write(f"\n## {s['note']}\n\n" + re.sub(r"^# .*\n+", "", s["from"].read_text()))
        elif kind == "format" and s["from"] != dst and ((tracked := _tracked(folder, s["from"])) or same(s["from"], dst)):
            s["from"].write_text(s["text"])  # one file under two spellings on this volume, or a tracked file whose
            _rename_case(folder, s["from"], dst, tracked)  # index entry must follow on any volume
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(s["text"])
            if kind == "format" and s["from"] != dst:
                s["from"].unlink()
        done.append(f"{kind}: {s['from'].relative_to(folder) if 'from' in s and s['from'].is_relative_to(folder) else s.get('from', '')} -> {dst.relative_to(folder)}")
    return done


def summary(planned: dict) -> str:
    counts = {}
    for s in planned["steps"]:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    lines = [f"migrate {planned['folder']} -> {planned['ws'].relative_to(planned['folder'])}/ (vcs {planned['vcs']})"]
    lines += [f"  {k}: {n}" for k, n in counts.items()]

    def shown(s: dict) -> str:  # paths print relative to the folder; a file rewritten in place prints once
        rel = [str(p.relative_to(planned["folder"]) if isinstance(p, Path) and p.is_relative_to(planned["folder"]) else p)
               for p in (s.get("from", s["to"]), s["to"])]
        return f"  {s['kind']}{' (' + s['how'] + ')' if 'how' in s else ''} {' -> '.join(dict.fromkeys(rel))}"
    lines += [shown(s) for s in planned["steps"]
              if s["kind"] in ("relocate", "reference", "unlink") or s["kind"] == "lift" and s["from"].parent == planned.get("legacy")]
    folded = {}  # a legacy folder that folds in file by file prints once: procedures/ into knowledge/procedures/
    for s in planned["steps"]:
        if s["kind"] == "lift" and (legacy := planned.get("legacy")) and s["from"].parent != legacy:
            rel = s["from"].relative_to(legacy)
            top = shown({"kind": "lift", "how": s["how"], "from": legacy / rel.parts[0], "to": s["to"].parents[len(rel.parts) - 2]})
            folded[top] = folded.get(top, 0) + 1
    lines += [f"{line} ({n} files fold in)" if n > 1 else f"{line} (1 file folds in)" for line, n in folded.items()]
    if links := sum(1 for s in planned["steps"] if s.get("link")):
        lines.append(f"  links left at the old paths: {links}")
    return "\n".join(lines + [f"  {'collision' if planned.get('collisions') else 'review'}: {r}" for r in planned["review"]])
