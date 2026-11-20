"""`<crew> update`: is the crew's clone behind its remote, and is its crewkit pin behind crewkit's newest tag?

It reports and changes nothing: a crew is tested against its pin, so moving the pin is a crew change (bump, test,
reinstall, push), never something a session start does on its own. A skill runs it first thing. The answer is kept in
~/.config/<crew>/update-check.json for the rest of the day while the clone's commit and its pin are the ones it
checked, so a busy day asks GitHub once and a pull asks again. A check that can't reach GitHub says so and steps aside.
"""
import datetime
import json
import re
import shlex
import subprocess
import tomllib
from pathlib import Path

from crewkit.install import InstallError, _kind

TIMEOUT = 8  # seconds per git call


def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT)
    if r.returncode:
        raise OSError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "git failed")
    return r.stdout


def _version(tag: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(map(int, m.groups())) if m else None


def pin(root: Path) -> tuple[str, str] | None:
    """(git url, tag) of the crewkit source the crew's pyproject pins, or None when it pins none."""
    src = tomllib.loads((root / "pyproject.toml").read_text()).get("tool", {}).get("uv", {}).get("sources", {})
    s = src.get("crewkit") or {}
    return (s["git"], s["tag"]) if s.get("git") and s.get("tag") else None


def newest_tag(url: str, git=_git) -> str | None:
    tags = [l.rsplit("refs/tags/", 1)[-1] for l in git("ls-remote", "--tags", "--refs", url).splitlines()]
    tags = [t for t in tags if _version(t)]
    return max(tags, key=_version) if tags else None


def newer_tag(url: str, pinned: str, git=_git) -> str | None:
    """The newest x.y.z tag at `url` when it is newer than `pinned`, else None. ValueError when the pin or the remote
    has no x.y.z tag, since there is nothing to compare; OSError when the remote can't be read."""
    if not _version(pinned):
        raise ValueError(f"the pin {pinned} is not an x.y.z tag")
    new = newest_tag(url, git)
    if not new:
        raise ValueError(f"{url} has no x.y.z tag")
    return new if _version(new) > _version(pinned) else None


def _ancestor(root: Path, older: str, newer: str, git) -> bool:
    try:
        git("merge-base", "--is-ancestor", older, newer, cwd=root)
        return True
    except OSError:
        return False


def clone_state(root: Path, git=_git) -> str:
    """'current', 'ahead', 'behind' or 'diverged', comparing HEAD with its upstream's tip on the remote, or 'unfetched'
    when the clone lacks that tip: it never fetches, so it can't tell behind from diverged then."""
    up = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=root).strip()  # origin/main
    remote, branch = up.split("/", 1)
    heads = git("ls-remote", remote, f"refs/heads/{branch}", cwd=root).split()
    if not heads:
        raise InstallError(f"{remote} has no branch {branch}, which {root} tracks; point the clone at another "
                           f"(`git -C {shlex.quote(str(root))} branch --set-upstream-to=<remote>/<branch>`) and run "
                           "update again")
    theirs = heads[0]
    ours = git("rev-parse", "HEAD", cwd=root).strip()
    if theirs == ours:
        return "current"
    try:
        git("cat-file", "-e", f"{theirs}^{{commit}}", cwd=root)
    except OSError:
        return "unfetched"
    if _ancestor(root, theirs, ours, git):
        return "ahead"
    return "behind" if _ancestor(root, ours, theirs, git) else "diverged"


def _key(root: Path, pinned: tuple[str, str] | None, today: str, git) -> dict:
    """What a check looks at: the day, the clone and the commit it is on, and the pin. A kept answer serves only the
    same key, so a pull or a new pin asks again."""
    try:
        head = git("rev-parse", "HEAD", cwd=root).strip()
    except (OSError, subprocess.TimeoutExpired):
        head = None  # not a clone: the check says so and keeps nothing
    return {"date": today, "root": str(root), "head": head, "pin": list(pinned) if pinned else None}


def _kept(cache: Path, key: dict, cmd: str) -> list[str] | None:
    """The answer kept for `key`, or None when there is none to reuse. InstallError, one line and nothing written, for
    a kept answer in a shape this code did not write."""
    again = f"; delete it, or run `{cmd} update --force` to ask again and replace it"
    try:
        text = cache.read_text()
    except (OSError, ValueError) as e:
        raise InstallError(f"{cache} cannot be read ({e}){again}") from e
    try:
        kept = json.loads(text)
    except ValueError:
        return None  # not JSON at all: asked again and replaced, as it always was
    if not isinstance(kept, dict):
        raise InstallError(f"{cache} holds {_kind(kept)}, not a kept answer{again}")
    if any(kept.get(k) != v for k, v in key.items()):
        return None
    lines = kept.get("lines")
    if not isinstance(lines, list) or not all(isinstance(l, str) for l in lines):
        raise InstallError(f"{cache} holds a kept answer with no list of lines{again}")
    return [*lines, f"(checked earlier today; `{cmd} update --force` asks again)"]


def _headline(behind: bool, checked: list[str], unchecked: list[str]) -> str:
    """Names what was and was not looked at, since "up to date" would claim more than the check did."""
    if behind:
        return "update: something is behind"
    if not unchecked:
        return "update: up to date"
    if not checked:
        return f"update: not checked: {' and '.join(unchecked)}"
    return f"update: {' and '.join(checked)} not behind; {' and '.join(unchecked)} not checked"


def check(root: Path, name: str, cache: Path | None = None, force: bool = False, today: str | None = None,
          git=_git, *, command: str | None = None) -> list[str]:
    """Report lines; the first says whether anything is behind, and what it could not check. Never raises for a
    network failure. Raises InstallError, one line and nothing written, for a kept answer in a shape this code did
    not write and for a tracked branch the remote no longer has. Each hint names `command` (default `name`), what
    the owner types to reach the crew's installer."""
    cmd, p = command or name, pin(root)
    key = _key(root, p, today or datetime.date.today().isoformat(), git) if cache else {}
    if cache and not force and cache.is_file() and (kept := _kept(cache, key, cmd)) is not None:
        return kept
    lines, behind, checked, unchecked = [], False, [], []
    try:
        state, q = clone_state(root, git), shlex.quote(str(root))
        behind |= state not in ("current", "ahead")
        checked.append("the clone")
        said = {"behind": f"behind; `git -C {q} pull --ff-only`, then `{cmd} install`",
                "unfetched": f"the remote has moved on since the last fetch, so behind or diverged; `git -C {q} fetch`,"
                             f" then `{cmd} update --force` tells which"}
        lines.append(f"{name} clone: {said.get(state, state)}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        unchecked.append("the clone")
        lines.append(f"{name} clone: not checked ({e})")
    if p:
        try:
            new = newer_tag(*p, git)
            checked.append("the crewkit pin")
            behind |= bool(new)
            lines.append(f"crewkit: {name} pins {p[1]}, newest is {new}; bump the tag in pyproject.toml, `uv lock`, "
                         f"run the tests, `{cmd} install`" if new else f"crewkit: {p[1]} is the newest")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            unchecked.append("the crewkit pin")
            lines.append(f"crewkit: not checked ({e})")
    lines.insert(0, _headline(behind, checked, unchecked))
    if cache and not unchecked:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({**key, "lines": lines}))
        except OSError as e:
            lines.append(f"(answer not kept in {cache}: {e})")
    return lines
