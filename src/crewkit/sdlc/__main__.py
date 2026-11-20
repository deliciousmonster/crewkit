"""`crewkit sdlc status | next | lint | init | approve | change | retire | guide`. Exit 0 clean, 1 findings or a
refusal, 2 usage. A crew adds it to its console script with `"sdlc": "crewkit.sdlc"` in its dispatch table."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from crewkit import sdlc
from crewkit.sdlc.doc import digest
from crewkit.sdlc.lint import lint


def workspace(path: Path) -> Path:
    """The workspace a path names: itself when it holds a brief.md or projects/, else its agents/ when that exists."""
    p = path.expanduser().resolve()
    if not (p / "brief.md").exists() and not (p / "projects").is_dir() and (p / "agents").is_dir():
        return p / "agents"
    return p


def chosen(ws: Path, slug: str | None, *, all_ok: bool) -> list[Path]:
    if slug:
        return [ws / "projects" / slug]
    found = sdlc.projects(ws)
    if all_ok:
        return found
    return [ws]


def _status(found: list[Path], ws: Path) -> int:
    if not found:
        print(sdlc.line(ws))
        return 0
    for p in found:
        st = sdlc.status(p)
        gates = ", ".join(f"{sdlc.GATES[g]} {'rev ' + str(r) if r else 'open'}" for g, r in st.gates.items())
        errors = sum(f.level == "error" for f in st.findings)
        print(f"{p.relative_to(ws) if p != ws else '.'}: stage {st.stage}; {gates}; "
              f"{errors} error(s), {len(st.findings) - errors} warning(s)")
        brief = sdlc.Set(p).brief
        print(f"  brief_hash: {digest(brief.body) if brief else '-'}")
        print(f"  next: {st.next}")
    return 0


def _lint(found: list[Path], ws: Path, strict: bool, as_json: bool) -> int:
    if not found:
        print(sdlc.line(ws))
        return 1
    results = {str(p.relative_to(ws)) if p != ws else ".": lint(p) for p in found}
    if as_json:
        print(json.dumps({k: [asdict(f) for f in v] for k, v in results.items()}, indent=2))
    else:
        for k, findings in results.items():
            prefix = "" if k == "." else f"{k}/"
            print("\n".join(f"{f.level}: {prefix}{f.path}: {f.message} [{f.code}]" for f in findings) or f"{k}: clean")
    failing = [f for v in results.values() for f in v if f.level == "error" or strict]
    return 1 if failing else 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="crewkit sdlc", description="The agent SDLC: stage, next gate, lint, approvals.")
    ap.add_argument("--ws", type=Path, default=Path("."), help="the workspace (default: ./agents, or .)")
    ap.add_argument("--project", help="a project under projects/ (default: every project; the root for writes)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("next")
    p = sub.add_parser("lint")
    p.add_argument("--strict", action="store_true", help="warnings fail too")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("init")
    p.add_argument("--profile", choices=("full", "light"), default="full")
    p = sub.add_parser("approve")
    p.add_argument("gate", help="requirements, design, acceptance or CR-<n>")
    p.add_argument("--by", default="owner")
    p.add_argument("--words", required=True, help="the approver's own words, quoted")
    p.add_argument("--kind", choices=("approved", "adopted"), default="approved")
    p = sub.add_parser("change")
    p.add_argument("title")
    p.add_argument("--affects", required=True, help="REQ ids, comma separated")
    p.add_argument("--why", default="")
    p.add_argument("--by", default="owner")
    p = sub.add_parser("retire")
    p.add_argument("paths", nargs="+", help="files relative to the workspace")
    sub.add_parser("guide")
    return ap


def _write(a: argparse.Namespace, ws: Path) -> None:
    project = chosen(ws, a.project, all_ok=False)[0]
    if a.cmd == "init":
        written = sdlc.scaffold(project, a.profile)
        print("\n".join(f"wrote {project / w}" for w in written) or "init: every deliverable exists; nothing written")
    elif a.cmd == "approve":
        print(f"approve {a.gate}: signed in {sdlc.approve(project, a.gate, a.by, a.words, a.kind)}")
    elif a.cmd == "change":
        print(sdlc.change(project, a.title, [x.strip() for x in a.affects.split(",") if x.strip()], a.why, a.by))


def main(argv: list[str] | None = None) -> int:
    try:
        a = parser().parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    ws = workspace(a.ws)
    found = chosen(ws, a.project, all_ok=True)
    try:
        if a.cmd == "status":
            return _status(found, ws)
        if a.cmd == "lint":
            return _lint(found, ws, a.strict, a.json)
        if a.cmd == "guide":
            print(sdlc.guide(), end="")
        elif a.cmd == "next":
            print("\n".join(sdlc.next_gate(p) for p in found) or sdlc.line(ws))
        elif a.cmd == "retire":
            print("\n".join(sdlc.retire(ws, a.paths)))
        else:
            _write(a, ws)
    except sdlc.Refused as e:
        print(f"sdlc: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
