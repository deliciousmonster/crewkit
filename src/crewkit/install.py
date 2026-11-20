"""Install a crew's Claude Code skills from its clone: `crewkit install | uninstall | doctor | update`.

The crew declares what it ships in a [tool.crewkit] table of its own pyproject.toml, which crewkit.spec reads; this
module holds the mechanics once. Standard library plus pyyaml only, so it runs without the `run` extra. macOS and
Linux; Windows is unsupported (the skills are symlinked).

Every path an install creates is recorded in ~/.config/<crew>/install.json, and uninstall removes only those, and only
while they are still as the install left them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

from crewkit.spec import InstallError, Spec, bin_dir, config_dir, doctor, find_root, home, load_spec, manifests
from crewkit.spec import resolve_crew_path as resolve_crew_path  # re-exported: the README names it here

RESTART = "restart Claude Code (or open a new session) to load the skill"


def launcher(spec: Spec, script: str) -> str:
    """A launcher any POSIX shell runs as one word, so no agent keeps `uv run --project ...` in a variable. The clone
    path is shell-quoted, so a quote, `$` or backtick in it reaches uv as written."""
    return (f"#!/bin/sh\n# Installed by `{spec.command} install` (crewkit); `{spec.command} uninstall` removes it.\n"
            f'exec uv run --quiet --project {shlex.quote(str(spec.root))} {script} "$@"\n')


# ---- install ------------------------------------------------------------------------------------

def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _same_link(link: Path, target: Path) -> bool:
    return link.is_symlink() and Path(os.path.realpath(link)) == target.resolve()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _read_object(p: Path, outcome: str) -> dict:
    """settings.json or install.json as a dict, {} when absent; a link to nothing or anything unreadable stops
    rather than being overwritten. `outcome` ("nothing installed" or "nothing removed") ends the message."""
    if p.is_symlink() and not p.exists():
        raise InstallError(f"{p} is a link to {os.readlink(p)}, which is not there, {outcome}; fix or remove the link")
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except ValueError as e:
        raise InstallError(f"{p} is not valid JSON ({e}), {outcome}; fix it by hand")
    if not isinstance(data, dict):
        raise InstallError(f"{p} is not a JSON object, {outcome}; fix it by hand")
    return data


def _kind(value) -> str:
    """What a parsed JSON value is, for a message: 'null', 'a list', 'an object', 'a string', 'a number', 'a boolean'."""
    return ("null" if value is None else "a list" if isinstance(value, list) else "an object" if isinstance(value, dict)
            else "a string" if isinstance(value, str) else "a boolean" if isinstance(value, bool) else "a number")


def _shape(settings: Path, data: dict, events: set[str]) -> list[str]:
    """The doctor's [FAIL] rows for each part of a parsed settings.json that install or uninstall reads or writes and
    that holds the wrong type: `hooks` an object, each of `events` (the ones the crew declares or an earlier install
    recorded) a list, `permissions` an object and its `allow` a list. Empty when nothing it touches is the wrong
    shape. A part the crew never touches is not checked, so it is never a reason to refuse."""
    out = []

    def row(what: str, value, want: str) -> None:
        out.append(f"[FAIL] {settings}: {what} is {_kind(value)}, not {want} - fix it by hand")

    if "hooks" in data:
        if not isinstance(data["hooks"], dict):
            row("hooks", data["hooks"], "an object")
        else:
            for event in sorted(events):
                if event in data["hooks"] and not isinstance(data["hooks"][event], list):
                    row(f"hooks.{event}", data["hooks"][event], "a list")
    if "permissions" in data:
        if not isinstance(data["permissions"], dict):
            row("permissions", data["permissions"], "an object")
        elif "allow" in data["permissions"] and not isinstance(data["permissions"]["allow"], list):
            row("permissions.allow", data["permissions"]["allow"], "a list")
    return out


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")


def _strs(d, *keys: str) -> bool:
    return isinstance(d, dict) and all(isinstance(d.get(k), str) for k in keys)


def _list_of(v, fits) -> bool:
    return isinstance(v, list) and all(map(fits, v))


def _hook_record(h) -> bool:
    """{"event", "group"}, or the bare group installs before 0.6.0 recorded."""
    return isinstance(h, dict) and ("event" not in h
                                    or isinstance(h["event"], str) and isinstance(h.get("group"), dict))


# Each list install.json holds and what each item must be: links and agent or launcher files it wrote, settings
# entries it added, and paths --force moved aside. install and uninstall act on these, so another shape stops both.
RECORD = {"links": lambda i: _strs(i, "path", "target"), "files": lambda i: _strs(i, "path", "sha256"),
          "settings": lambda i: _strs(i, "file") and _list_of(i.get("hooks", []), _hook_record)
          and _list_of(i.get("permissions", []), lambda p: isinstance(p, str)),
          "backups": lambda i: isinstance(i, str)}


def _load_manifest(crew: str, outcome: str) -> dict:
    p = config_dir(crew) / "install.json"
    m = _read_object(p, outcome)
    m.setdefault("crew", crew)
    if bad := [k for k, fits in RECORD.items() if not _list_of(m.setdefault(k, []), fits)]:
        raise InstallError(f"{p} holds {' and '.join(bad)} in a shape install never writes, {outcome}; fix it by hand")
    return m


def _remember(items: list, key: str, item: dict) -> None:
    """Record `item`, replacing an earlier record with the same `key` value."""
    items[:] = [i for i in items if i.get(key) != item[key]] + [item]


def _link_state(link: Path, target: Path) -> str:
    """"same" when `link` already points at `target`, "other" when anything else is there, "new" when nothing is."""
    if _same_link(link, target):
        return "same"
    return "other" if link.is_symlink() or link.exists() else "new"


def _link_clash(link: Path, target: Path) -> str:
    return f"{link} already exists and is not a link to {target}"


def _link(target: Path, link: Path, force: bool, man: dict, said: list[str]) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    state = _link_state(link, target)
    if state == "same":
        said.append(f"already linked {link}")
    elif state == "other":
        if not force:
            raise InstallError(f"{_link_clash(link, target)}; remove it or use --force")
        aside = link.with_name(f"{link.name}.bak-{_stamp()}")
        link.rename(aside)
        said.append(f"moved {link} aside to {aside}")
        man["backups"].append(str(aside))
        link.symlink_to(target)
        said.append(f"linked {link} -> {target}")
    else:
        link.symlink_to(target)
        said.append(f"linked {link} -> {target}")
    _remember(man["links"], "path", {"path": str(link), "target": str(target.resolve())})


def _hook_groups(spec: Spec) -> list[tuple[str, dict]]:
    """(event, settings.json hook group) for each hook the crew declares, `hook` as SessionStart. A hook is shell
    text, so `{crew_path}` goes in shell-quoted; a permission and an agent file take it as written."""
    declared = ([{"event": "SessionStart", "command": spec.hook}] if spec.hook else []) + spec.hooks
    root = shlex.quote(str(spec.root))
    out = []
    for h in declared:
        group = {"matcher": h["matcher"]} if h.get("matcher") else {}
        group["hooks"] = [{"type": "command", "command": h["command"].replace("{crew_path}", root)}]
        out.append((h["event"], group))
    return out


def _recorded_hook(h: dict) -> tuple[str, dict]:
    # Installs before 0.6.0 recorded a bare SessionStart group.
    return (h["event"], h["group"]) if "event" in h else ("SessionStart", h)


def _recorded_hooks(rec: dict) -> list[tuple[str, dict]]:
    return [_recorded_hook(h) for h in rec.get("hooks", [])]


def _events(declared: list[tuple[str, dict]], rec: dict | None) -> set[str]:
    """The hook events an install or uninstall of one settings file reads or writes."""
    return {e for e, _ in declared} | {e for e, _ in _recorded_hooks(rec or {})}


def _items(owner: dict, key: str, bad: list[str]) -> list:
    """owner[key] as a list: empty when the key is missing, and empty and named in `bad` when it is not a list."""
    value = owner.get(key, [])
    if isinstance(value, list):
        return value
    bad.append(f"{key} is {'null' if value is None else 'not a list'}")
    return []


def _read_others(crew: str, said: list[str]) -> list[tuple[object, list[tuple[str, dict]], list]]:
    """What each other installed crew's install.json records, as (settings file, hook groups, permissions).
    Read once, before anything changes, and defensively: a missing or null list counts as empty and a hook
    record that names no group protects nothing. A manifest read that way is named in `said`."""
    out = []
    for path, man in manifests():
        if path.parent.name == crew:
            continue
        if not isinstance(man, dict):
            said.append(f"ignored {path}: it is not a JSON object")
            continue
        bad: list[str] = []
        for s in _items(man, "settings", bad):
            if not isinstance(s, dict):
                bad.append("a settings record is not an object")
                continue
            hooks = []
            for h in _items(s, "hooks", bad):
                if not isinstance(h, dict):
                    bad.append("a hook record is not an object")
                elif "event" in h and not isinstance(h.get("group"), dict):
                    bad.append("a hook record names no group")
                else:
                    hooks.append(_recorded_hook(h))
            out.append((s.get("file"), hooks, _items(s, "permissions", bad)))
        if bad:
            said.append(f"ignored malformed parts of {path}: {'; '.join(dict.fromkeys(bad))}")
    return out


def _held(others: list, settings: Path) -> tuple[list[tuple[str, dict]], list]:
    """The hook groups and permissions the other crews' manifests record in this settings file."""
    hooks, perms = [], []
    for file, h, p in others:
        if file == str(settings):
            hooks += h
            perms += p
    return hooks, perms


def _drop(data: dict, hooks: list[tuple[str, dict]], perms: list[str]) -> None:
    """Take these hook groups and permissions out of a settings dict, and any key that empties."""
    groups_by_event = data.get("hooks", {})
    for event, group in hooks:
        groups = groups_by_event.get(event, [])
        if group in groups:
            groups.remove(group)
        if event in groups_by_event and not groups:
            del groups_by_event[event]
    if "hooks" in data and not groups_by_event:
        del data["hooks"]
    allowed = data.get("permissions", {})
    if "allow" in allowed:
        allowed["allow"] = [a for a in allowed["allow"] if a not in perms]
        if not allowed["allow"]:
            del allowed["allow"]
        if not allowed:
            del data["permissions"]


def _rewrite(p: Path, data: dict, before: str, verb: str, said: list[str]) -> None:
    """Write `data` to the settings file `p` when it differs from `before`, backing up what was there first."""
    if json.dumps(data, sort_keys=True) == before:
        return
    backup = p.with_name(f"{p.name}.bak-{_stamp()}") if p.exists() else None
    if backup:
        shutil.copy2(p, backup)
    _write_json(p, data)
    said.append(f"{verb} {p}" + (f" (backup {backup})" if backup else ""))


def _settings_edit(settings: Path, data: dict, others: list, spec: Spec, man: dict, said: list[str]) -> None:
    """Merge the crew's hooks and permissions into `data`, the settings.json install() read before it changed anything."""
    perms = [p.replace("{crew_path}", str(spec.root)) for p in spec.permissions]
    declared = _hook_groups(spec)
    before = json.dumps(data, sort_keys=True)
    old = next((s for s in man["settings"] if s["file"] == str(settings)), None)
    rec = old or {"file": str(settings), "hooks": [], "permissions": []}
    rec.setdefault("permissions", [])
    # A recorded entry the crew no longer declares goes, so a changed matcher replaces its group instead of
    # joining it; one the user had was never recorded, and one another crew still records stays.
    recorded = _recorded_hooks(rec)
    held_hooks, held_perms = _held(others, settings)
    _drop(data, [h for h in recorded if h not in declared and h not in held_hooks],
          [p for p in rec["permissions"] if p not in perms and p not in held_perms])
    rec["hooks"] = [{"event": e, "group": g} for e, g in recorded if (e, g) in declared]
    rec["permissions"] = [p for p in rec["permissions"] if p in perms]
    # Record what this install adds, and what another crew's manifest already holds so that crew
    # dropping it leaves it; an entry the user already had stays theirs on uninstall.
    for event, group in declared:
        groups = data.setdefault("hooks", {}).setdefault(event, [])
        added = group not in groups
        if added:
            groups.append(group)
        if (added or (event, group) in held_hooks) and {"event": event, "group": group} not in rec["hooks"]:
            rec["hooks"].append({"event": event, "group": group})
    if perms:
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        for p in perms:
            added = p not in allow
            if added:
                allow.append(p)
            if (added or p in held_perms) and p not in rec["permissions"]:
                rec["permissions"].append(p)
    _rewrite(settings, data, before, "updated", said)
    if old is None and (rec["hooks"] or rec["permissions"]):
        man["settings"].append(rec)


def _text(p: Path) -> str | None:
    """What a file holds, or None when it is not a readable text file (a folder, or bytes that are not UTF-8)."""
    try:
        return p.read_text() if p.is_file() else None
    except (OSError, ValueError):
        return None


def _file_state(dest: Path, text: str, man: dict) -> str:
    """"new" when nothing is at `dest`; "same" when it holds `text`; "ours" when it holds what an earlier install
    wrote; "foreign" when it holds anything else, is not a readable text file, or is a link to nothing."""
    if not dest.exists():
        # A link whose target is missing is somebody's: writing would go through it, or fail after other changes.
        return "foreign" if dest.is_symlink() else "new"
    held = _text(dest)
    if held == text:
        return "same"
    recorded = {f["path"]: f["sha256"] for f in man["files"]}
    return "ours" if held is not None and recorded.get(str(dest)) == _sha(held) else "foreign"


def _file_clash(dest: Path, spec: Spec) -> str:
    return f"{dest} already exists and is not {spec.name}'s as installed"


def _place(dest: Path, text: str, spec: Spec, force: bool, man: dict, said: list[str], mode: int | None = None) -> None:
    """Write a file the install owns, moving aside (with --force) one it did not write."""
    state = _file_state(dest, text, man)
    if state == "foreign":
        if not force:
            raise InstallError(f"{_file_clash(dest, spec)}; remove it or use --force")
        aside = dest.with_name(f"{dest.name}.bak-{_stamp()}")
        dest.rename(aside)
        said.append(f"moved {dest} aside to {aside}")
        man["backups"].append(str(aside))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if state != "same":
        dest.write_text(text)
        said.append(f"wrote {dest}")
    if mode is not None:
        dest.chmod(mode)
    _remember(man["files"], "path", {"path": str(dest), "sha256": _sha(text)})


def _blocker(path: Path) -> Path | None:
    """The nearest existing ancestor of `path` that is not a folder, which stops the folders above `path` being made."""
    for d in path.parents:
        if d.exists() or d.is_symlink():
            return None if d.is_dir() else d
    return None


def _preflight(spec: Spec, links: list[tuple[Path, Path]], files: list[tuple[Path, str, int | None]],
               force: bool, man: dict) -> list[str]:
    """Every reason this install cannot finish, in the doctor's row form, found before anything changes: a link or
    an agent or launcher file that is not ours (unless `force` moves it aside), a path whose folder cannot be
    made, and an agents_dir that is not there. Empty when the install will run to its end."""
    out, fix = [], "remove it or use --force"
    if spec.agents_dir and not (spec.root / spec.agents_dir).is_dir():
        out.append(f"[FAIL] agents_dir {spec.root / spec.agents_dir} does not exist - check [tool.crewkit] agents_dir")
    for target, link in links:
        if blocker := _blocker(link):
            out.append(f"[FAIL] {link} cannot be made, {blocker} is not a folder - move {blocker} aside")
        elif _link_state(link, target) == "other" and not force:
            out.append(f"[FAIL] {_link_clash(link, target)} - {fix}")
    for dest, text, _ in files:
        if blocker := _blocker(dest):
            out.append(f"[FAIL] {dest} cannot be made, {blocker} is not a folder - move {blocker} aside")
        elif _file_state(dest, text, man) == "foreign" and not force:
            out.append(f"[FAIL] {_file_clash(dest, spec)} - {fix}")
    return out


def _planned(spec: Spec, claude: Path) -> tuple[list, list]:
    """The skill links an install makes, as (target, link), and the agent and launcher files, as (path, text, mode)."""
    links = [(spec.root / spec.skills_dir / s, claude / "skills" / s) for s in spec.skills]
    for target, _ in links:
        if not (target / "SKILL.md").is_file():
            raise InstallError(f"{target} has no SKILL.md; check [tool.crewkit] skills")
    agents = spec.root / spec.agents_dir if spec.agents_dir else None
    files = [(claude / "agents" / f.name, f.read_text().replace("{crew_path}", str(spec.root)), None)
             for f in sorted(agents.glob("*.md"))] if agents and agents.is_dir() else []
    return links, files + [(bin_dir() / script, launcher(spec, script), 0o755) for script in spec.bin]


def install(spec: Spec, scope: str = "user", target: Path | None = None, force: bool = False,
            which=None) -> list[str]:
    """Link the crew's skills, and copy its agents and merge its settings when it declares them."""
    if scope not in ("user", "project"):
        raise InstallError("--scope is user or project")
    if scope == "project" and target is None:
        raise InstallError("--scope project needs --target DIR")
    code, report = doctor(spec, which)
    said = list(report)
    if code:
        raise InstallError("prerequisites missing, nothing installed:\n" + "\n".join(report))
    claude = home() / ".claude" if scope == "user" else Path(target).resolve() / ".claude"
    links, files = _planned(spec, claude)
    # Read every input before anything changes: a record or settings.json that will not parse stops the install here.
    man = _load_manifest(spec.name, "nothing installed")
    settings, data, others, wrong = claude / "settings.json", None, [], []
    if spec.hook or spec.hooks or spec.permissions or any(s["file"] == str(settings) for s in man["settings"]):
        data = _read_object(settings, "nothing installed")
        others = _read_others(spec.name, said)
        old = next((s for s in man["settings"] if s["file"] == str(settings)), None)
        wrong = _shape(settings, data, _events(_hook_groups(spec), old))
    # Check the shape of what it will edit, then every link, agent file, launcher and agents_dir, so a conflict stops
    # the install with nothing changed.
    if problems := wrong + _preflight(spec, links, files, force, man):
        raise InstallError("conflicts found, nothing installed:\n" + "\n".join(problems))
    cfg = config_dir(spec.name)
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "crew-path").write_text(str(spec.root) + "\n")
    for target, link in links:
        _link(target, link, force, man, said)
    for dest, text, mode in files:
        _place(dest, text, spec, force, man, said, mode)
    if data is not None:
        _settings_edit(settings, data, others, spec, man, said)
    _write_json(cfg / "install.json", man)
    said += [f"crew path {spec.root}", RESTART]
    return said


def _remove_paths(man: dict, said: list[str]) -> None:
    """Remove each recorded link still pointing at the clone and each recorded file still as written; name the rest."""
    for l in man["links"]:
        p, target = Path(l["path"]), Path(l["target"])
        if _same_link(p, target):
            p.unlink()
            said.append(f"removed {p}")
        elif p.is_symlink() or p.exists():
            said.append(f"left {p}: it no longer links to {target}")
    for f in man["files"]:
        p, held = Path(f["path"]), _text(Path(f["path"]))
        if held is not None and _sha(held) == f["sha256"]:
            p.unlink()
            said.append(f"removed {p}")
        elif p.is_symlink() or p.exists():
            said.append(f"left {p}: it was changed after the install")


def uninstall(spec_or_name: Spec | str) -> list[str]:
    """Remove what install.json records, and only while it is still as the install left it: a link
    that still points at the crew, an agent file whose text is unchanged, and the settings entries
    the install added. Anything else is left in place and named."""
    name = spec_or_name if isinstance(spec_or_name, str) else spec_or_name.name
    path = config_dir(name) / "install.json"
    if not (path.exists() or path.is_symlink()):
        return [f"nothing recorded for {name}"]
    said = []
    # Read every input before anything changes: a record or settings.json that will not parse stops the uninstall here.
    man = _load_manifest(name, "nothing removed")
    parsed = {s["file"]: _read_object(Path(s["file"]), "nothing removed") for s in man["settings"]}
    others = _read_others(name, said)
    if wrong := [r for s in man["settings"] for r in _shape(Path(s["file"]), parsed[s["file"]], _events([], s))]:
        raise InstallError("conflicts found, nothing removed:\n" + "\n".join(dict.fromkeys(wrong)))
    _remove_paths(man, said)
    for s in man["settings"]:
        p, data = Path(s["file"]), parsed[s["file"]]
        before = json.dumps(data, sort_keys=True)
        held_hooks, held_perms = _held(others, p)
        _drop(data, [h for h in _recorded_hooks(s) if h not in held_hooks],
              [x for x in s.get("permissions", []) if x not in held_perms])
        _rewrite(p, data, before, "cleaned", said)
    for b in man["backups"]:
        if Path(b).exists() or Path(b).is_symlink():
            said.append(f"left {b}: moved aside by install --force; restore it by hand if you want it back")
    path.unlink()
    (config_dir(name) / "crew-path").unlink(missing_ok=True)
    said.append(f"removed {path}")
    try:
        config_dir(name).rmdir()  # only when empty; a key file or anything else keeps it
    except OSError:
        pass
    return said


# ---- command line -------------------------------------------------------------------------------

def lint(spec: Spec) -> int:
    """`crewkit lint`: each tool a role's flows declare on a `**Tools:**` line that the role does not hold."""
    import yaml

    from crewkit.lint import check_flow_tools, flows_of

    if not spec.flows:
        raise InstallError("lint: [tool.crewkit] has no flows table (tools, crew, path); nothing to check")
    read = lambda key: yaml.safe_load((spec.root / spec.flows[key]).read_text()) or {}  # noqa: E731
    crew = read("crew")
    flows = flows_of(crew, spec.flows["path"], spec.root)
    found = check_flow_tools(read("tools"), flows, list(crew.get("roles") or {}))
    for line in found:
        print(f"[FAIL] {line.replace(f'{spec.root}/', '')}")
    count = sum(map(len, flows.values()))
    print(f"lint: {len(found)} problem{'s' * (len(found) != 1)} in {count} role flows" if found
          else f"lint: {count} role flows hold every tool they declare")
    return 1 if found else 0


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    """`crewkit <install|uninstall|doctor|update|lint>`. A crew's own CLI passes its `root` so its command is
    `<crew-cli> install`; run bare, the crew is the clone at or above --root (default: the cwd)."""
    ap = argparse.ArgumentParser(prog="crewkit", description="Install a crew's Claude Code skills from its clone.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("install", "uninstall", "doctor", "update", "lint"):
        p = sub.add_parser(cmd)
        if root is None:
            p.add_argument("--root", type=Path, help="the crew clone (default: found from the current folder)")
        if cmd == "install":
            p.add_argument("--scope", choices=("user", "project"), default="user")
            p.add_argument("--target", type=Path, help="project folder for --scope project")
            p.add_argument("--force", action="store_true", help="move a conflicting path aside and replace it")
        if cmd == "update":
            p.add_argument("--force", action="store_true", help="ask again even if checked earlier today")
    a = ap.parse_args(argv)
    spec = None
    try:
        spec = load_spec(root or find_root(a.root or Path.cwd()))
        if a.cmd == "doctor":
            code, lines = doctor(spec)
            print("\n".join(lines))
            return code
        if a.cmd == "lint":
            return lint(spec)
        if a.cmd == "update":
            from crewkit.update import check
            print("\n".join(check(spec.root, spec.name, config_dir(spec.name) / "update-check.json", a.force,
                                  command=spec.command)))
            return 0
        if a.cmd == "uninstall":
            print("\n".join(uninstall(spec)))
            return 0
        print("\n".join(install(spec, a.scope, a.target, a.force)))
        return 0
    except (InstallError, OSError) as e:
        where = f" (repo {spec.repo})" if spec and spec.repo else ""
        print(f"crewkit: {e}{where}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
