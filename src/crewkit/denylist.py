"""Private terms kept out of a public repository: a scan of its tracked files against a denylist kept outside it, and
a structural check that needs no list (home-directory paths and real-looking email addresses).

The list is `$CREWKIT_PRIVATE_TERMS` or `~/.config/crewkit/private-terms`: one case-insensitive regex per line, `#`
comments and blank lines ignored. A hit prints as `path:line` with the term's line number in the list, never the
term, so a failing log does not leak what it protects. A crew's test is three lines:

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
HOME = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)/")
HOME_OK = {"user", "username", "you", "me", "runner", "example", "name", "someone"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
EMAIL_OK = re.compile(r"(^|\.)(example\.(com|org|net)|users\.noreply\.github\.com)$|^github\.com$", re.I)


@dataclass
class Hit:
    path: str
    line: int
    what: str  # "term N" (its line in the list), "home path" or "email"

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what}"


def terms_path() -> Path:
    return Path(os.environ.get(ENV) or Path.home() / ".config/crewkit/private-terms").expanduser()


def load(path: Path) -> list[tuple[int, re.Pattern[str]]]:
    """The list's patterns with their line numbers; a bad one raises ValueError naming only its line."""
    out = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        term = raw.strip()
        if not term or term.startswith("#"):
            continue
        try:
            out.append((n, re.compile(term, re.I)))
        except re.error:
            raise ValueError(f"{path} line {n}: not a valid regex") from None
    return out


def tracked(root: Path) -> list[str]:
    done = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=True)
    return [p for p in done.stdout.split("\0") if p and Path(p).name not in EXCLUDED]


def _lines(root: Path):
    for rel in tracked(root):
        try:
            data = (root / rel).read_bytes()
        except OSError:  # a deleted file still in the index, or a submodule
            continue
        if b"\0" not in data:
            yield from ((rel, n, text) for n, text in enumerate(data.decode("utf-8", "replace").splitlines(), 1))


def scan(root: Path, terms: list[tuple[int, re.Pattern[str]]]) -> list[Hit]:
    return [Hit(rel, n, f"term {t}") for rel, n, text in _lines(Path(root)) for t, rx in terms if rx.search(text)]


def structural(root: Path) -> list[Hit]:
    """Home-directory paths and email addresses outside the example and noreply domains; needs no list."""
    hits = []
    for rel, n, text in _lines(Path(root)):
        if any(m.group(1).lower() not in HOME_OK for m in HOME.finditer(text)):
            hits.append(Hit(rel, n, "home path"))
        if any(not EMAIL_OK.search(m.group(1)) for m in EMAIL.finditer(text)):
            hits.append(Hit(rel, n, "email"))
    return hits


def check(root: Path) -> tuple[list[Hit], str | None]:
    """Every hit, and why the term scan was skipped (no list), or None when it ran."""
    root = Path(root)
    hits = structural(root)
    path = terms_path()
    if not path.is_file():
        return hits, f"no private-terms list at {path} (set ${ENV}); the term scan is skipped"
    return hits + scan(root, load(path)), None


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
        hits, skipped = check(a.dir)
    except (ValueError, subprocess.CalledProcessError, OSError) as e:
        print(f"denylist: {e}", file=sys.stderr)
        return 2
    print("\n".join(map(str, hits)) or "denylist: no hits")
    if skipped:
        print(f"denylist: {skipped}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
