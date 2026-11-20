"""The model behind a tier, billed to an API key or to the Claude plan Claude Code is signed in to.

CREW_LLM_BACKEND: "api" bills an API key, "plan" sends each call through `claude -p`, and "auto" (the default) takes
the key when one is set or filed, and the plan otherwise.
"""
import os
from pathlib import Path

BACKENDS = ("auto", "api", "plan")


def key_present(key_file: Path | None) -> bool:
    """An empty ANTHROPIC_API_KEY is no key; a key file counts whatever it holds."""
    return bool(os.environ.get("ANTHROPIC_API_KEY")) or bool(key_file and key_file.exists())


def _pick(asked: str, has_key: bool) -> str:
    """The backend for a valid CREW_LLM_BACKEND value; shared by backend() and crewkit.agent_sdk.resolve()."""
    if asked != "auto":
        return asked
    return "api" if has_key else "plan"


def backend(key_file: Path | None = None) -> str:
    asked = os.environ.get("CREW_LLM_BACKEND", "auto")
    if asked not in BACKENDS:
        raise SystemExit("CREW_LLM_BACKEND must be auto, api or plan")
    return _pick(asked, key_present(key_file))


def model_id(models: dict, tier: str) -> str:
    runtime = models["runtimes"]["crewai"]
    return models["providers"][runtime["provider"]][tier]


def llm(models: dict, tier: str, key_file: Path | None = None):
    model = model_id(models, tier)
    if backend(key_file) == "plan":
        from crewkit.plan_llm import ClaudePlanLLM

        return ClaudePlanLLM(model=model)
    from crewai import LLM

    if not os.environ.get("ANTHROPIC_API_KEY"):
        if not (key_file and key_file.exists()):
            raise SystemExit(f"the api backend needs ANTHROPIC_API_KEY or {key_file}")
        # Read at the point of use, handed over in the environment, never printed.
        os.environ["ANTHROPIC_API_KEY"] = key_file.read_text().strip()
    return LLM(model=models["runtimes"]["crewai"].get("prefix", "") + model)
