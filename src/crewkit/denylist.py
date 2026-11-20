"""Private terms kept out of a public repository: a scan of its tracked files against a denylist kept outside it, and
a structural check that needs no list (home-directory paths and real-looking email addresses).

The list is `$CREWKIT_PRIVATE_TERMS` or `~/.config/crewkit/private-terms`: one case-insensitive regex per line, `#`
comments and blank lines ignored. A hit prints as `path:line` with the term's line number in the list, never the
term, so a failing log does not leak what it protects; a file whose name holds a term prints as `file #N`, its
index in `git ls-files`.

The structural check skips what `.crewkit-allow` at the repository root allows: one case-insensitive regex per
line, searched in each address or path found, for the public addresses a repository's own mechanism names. It never
applies to the private terms. A `noreply` or `no-reply` address needs no entry. A crew's test is three lines:

    class PrivateTerms(unittest.TestCase):
        def test_clean(self):
            denylist.assert_clean(self, ROOT)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ENV = "CREWKIT_PRIVATE_TERMS"
EXCLUDED = {"uv.lock"}
ALLOW = ".crewkit-allow"
HOME = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)/")
HOME_OK = {"user", "username", "you", "me", "runner", "example", "name", "someone"}
EMAIL = re.compile(r"([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
NOREPLY = re.compile(r"no-?reply", re.I)
EMAIL_OK = re.compile(r"(^|\.)(example\.(com|org|net)|users\.noreply\.github\.com)$|^github\.com$", re.I)


@dataclass
class Hit:
    path: str  # the tracked path, or "file #N" when the name itself holds a term
    line: int  # 0 for a hit in the name
    what: str  # "term N" (its line in the list), "home path" or "email"

    def __str__(self) -> str:
        return f"{self.path}{f':{self.line}' if self.line else ''}: {self.what}"


def terms_path() -> Path:
    return Path(os.environ.get(ENV) or Path.home() / ".config/crewkit/private-terms").expanduser()


@dataclass
class Report:
    hits: list[Hit]
    skipped: str | None  # why the term scan did not run
    notes: list[str]  # allow-file lines that allowed nothing


def load(path: Path) -> list[tuple[int, re.Pattern[str]]]:
    """A list's patterns with their line numbers; a bad one raises ValueError naming only its line."""
    out = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        term = raw.strip()
        if not term or term.startswith("#"):
            continue
        try:
            rx = re.compile(term, re.I)
        except re.error:
            raise ValueError(f"{path} line {n}: not a valid regex") from None
        if rx.search(""):
            raise ValueError(f"{path} line {n}: matches empty text, so it would match every line")
        out.append((n, rx))
    return out


def tracked(root: Path) -> list[str]:
    done = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=True)
    return [p for p in done.stdout.split("\0") if p and Path(p).name not in EXCLUDED]


Terms = list[tuple[int, re.Pattern[str]]]


def _files(root: Path, terms: Terms) -> list[tuple[str, str, list[Hit]]]:
    """Each tracked file as (path, its label in a report, the hits in its name)."""
    out = []
    for i, rel in enumerate(tracked(root), 1):
        named = [Hit(f"file #{i}", 0, f"term {t}") for t, rx in terms if rx.search(rel)]
        out.append((rel, f"file #{i}" if named else rel, named))
    return out


def _lines(root: Path, files: list[tuple[str, str, list[Hit]]]):
    for rel, label, _ in files:
        try:
            data = (root / rel).read_bytes()
        except OSError:  # a deleted file still in the index, or a submodule
            continue
        if b"\0" not in data:
            yield from ((label, n, text) for n, text in enumerate(data.decode("utf-8", "replace").splitlines(), 1))


def scan(root: Path, terms: Terms) -> list[Hit]:
    """Hits in file names and file contents; a path whose name holds a term is never printed."""
    files = _files(Path(root), terms)
    named = [h for _, _, hits in files for h in hits]
    return named + [Hit(label, n, f"term {t}") for label, n, text in _lines(Path(root), files) for t, rx in terms if rx.search(text)]


def structural(root: Path, terms: Terms | None = None, allow: Terms | None = None,
               used: set[int] | None = None) -> list[Hit]:
    """Home-directory paths and email addresses outside the example, noreply and allowed ones; needs no list.
    Each allow line that let something through is added to `used`."""
    used = set() if used is None else used

    def allowed(found: str) -> bool:
        lines = {n for n, rx in allow or [] if rx.search(found)}
        used.update(lines)
        return bool(lines)

    hits = []
    files = [f for f in _files(Path(root), terms or []) if f[0] != ALLOW]  # its own entries would mark it used
    for label, n, text in _lines(Path(root), files):
        # lists, not generators, so every allow line that matches is marked used
        if any([m.group(1).lower() not in HOME_OK and not allowed(m.group(0)) for m in HOME.finditer(text)]):
            hits.append(Hit(label, n, "home path"))
        if any([not (EMAIL_OK.search(m.group(2)) or NOREPLY.fullmatch(m.group(1)) or allowed(m.group(0)))
                for m in EMAIL.finditer(text)]):
            hits.append(Hit(label, n, "email"))
    return hits


def report(root: Path) -> Report:
    """Every hit, why the term scan was skipped (no list) or None, and the allow lines that matched nothing."""
    root, path = Path(root), terms_path()
    allow = load(root / ALLOW) if (root / ALLOW).is_file() else []
    used: set[int] = set()
    terms = load(path) if path.is_file() else None
    hits = structural(root, terms, allow, used) + (scan(root, terms) if terms is not None else [])
    notes = [f"{ALLOW} line {n} allowed nothing" for n, _ in allow if n not in used]
    skipped = None if terms is not None else f"no private-terms list at {path} (set ${ENV}); the term scan is skipped"
    return Report(hits, skipped, notes)


def check(root: Path) -> tuple[list[Hit], str | None]:
    """Every hit, and why the term scan was skipped (no list), or None when it ran."""
    r = report(root)
    return r.hits, r.skipped


def assert_clean(test: unittest.TestCase, root: Path) -> None:
    """Fail on any hit; skip, with the reason, when no list is installed and the structural check passed."""
    hits, skipped = check(root)
    if hits:
        test.fail("private content in tracked files:\n" + "\n".join(map(str, hits)))
    if skipped:
        test.skipTest(skipped)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crewkit denylist", description="Scan tracked files for private terms.")
    ap.add_argument("--dir", type=Path, default=Path("."), help="the repository (default: the current folder)")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    try:
        r = report(a.dir)
    except (ValueError, subprocess.CalledProcessError, OSError) as e:
        print(f"denylist: {e}", file=sys.stderr)
        return 2
    if r.skipped:
        print(f"denylist: {r.skipped}")
    for note in r.notes:
        print(f"denylist: note: {note}")
    print("\n".join(map(str, r.hits)) or "denylist: no hits")
    return 1 if r.hits else 0


if __name__ == "__main__":
    sys.exit(main())
