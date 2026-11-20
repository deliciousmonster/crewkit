# One console script per crew, with each tool a subcommand: `examplecrew reports run ...`. Agents then run a
# literal command every call instead of keeping it in a shell variable, which zsh does not word-split.
import runpy
import sys


def dispatch(commands: dict, argv: list | None = None, prog: str = "") -> int:
    """Run the module `commands` maps argv[0] to as __main__, with argv[1:] as its arguments."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in commands:
        width = max(map(len, commands))
        print(f"usage: {prog} <command> [args]\n" + "\n".join(f"  {n:<{width}}  {m}" for n, m in commands.items()),
              file=sys.stderr)
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    saved = sys.argv
    sys.argv = [f"{prog} {argv[0]}", *argv[1:]]
    try:
        runpy.run_module(commands[argv[0]], run_name="__main__")
    except SystemExit as e:
        if isinstance(e.code, str):  # a usage line, as sys.exit would print it
            print(e.code, file=sys.stderr)
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = saved
    return 0
