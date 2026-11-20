"""The command line every crew's main.py hands off to.

    [--dry-run] <one value per crew.yaml input, in order> <task> [<task> ...]

--dry-run, or CREW_DRY_RUN=1, builds the crew and prints it without calling a model.
"""
import os
import sys

from crewkit.builder import build_crew, describe
from crewkit.config import CrewConfig
from crewkit.llm import backend


def main(cfg: CrewConfig, argv: list[str] | None = None, registry=None, dry: bool = False, gates=None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--dry-run"]:
        dry, args = True, args[1:]
    dry = dry or os.environ.get("CREW_DRY_RUN") == "1"
    names = cfg.inputs
    if len(args) <= len(names):
        raise SystemExit(f"usage: [--dry-run] {' '.join(f'<{n}>' for n in names)} <task> [<task> ...]")
    inputs = dict(zip(names, args[: len(names)]))
    crew = build_crew(cfg, args[len(names):], inputs, registry, gates)
    if dry:
        print(f"config {cfg.config_dir.name}/, backend {backend(cfg.key_file)}")
        print(describe(crew))
    else:
        print(crew.kickoff(inputs=inputs))
