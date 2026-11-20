# A crew's configuration as the layout standard has it: agents.yaml and tasks.yaml in CrewAI's schema,
# and crew.yaml, models.yaml and tools.yaml beside them for what CrewAI's schema does not hold.
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _body(text: str) -> str:
    # A knowledge file may open with YAML frontmatter (summary, load_when); the text after it is the knowledge.
    if text.startswith("---\n"):
        return text.split("\n---\n", 1)[-1].lstrip("\n")
    return text


def _sections(text: str, headings: list[str]) -> str:
    # Only the named `## ` sections, after whatever precedes the first one, as crew.yaml's inline_sections asks.
    chunks = re.split(r"(?m)^(?=## )", text)
    keep = [c for c in chunks[1:] if any(c.startswith(f"## {h}\n") for h in headings)]
    return "".join([chunks[0], *keep]).strip()


@dataclass
class CrewConfig:
    config_dir: Path
    knowledge_dir: Path
    key_file: Path | None = None
    agents: dict = field(init=False)
    tasks: dict = field(init=False)
    crew: dict = field(init=False)
    models: dict = field(init=False)
    tools: dict = field(init=False)

    def __post_init__(self):
        for name in ("agents", "tasks", "crew", "models", "tools"):
            setattr(self, name, _load(self.config_dir / f"{name}.yaml"))

    @property
    def inputs(self) -> list[str]:
        return list(self.crew.get("inputs", []))

    @property
    def manager(self) -> str | None:
        return self.crew.get("manager")

    def knowledge(self, role: str, inputs: dict) -> str:
        # Inlined, not passed as knowledge_sources: CrewAI's knowledge store embeds documents, and the
        # crews run on Anthropic models alone.
        spec = self.crew.get("roles", {}).get(role, {})
        only = spec.get("inline_sections", {})
        texts = []
        for name in spec.get("knowledge", []):
            text = _body((self.knowledge_dir / f"{name}.md").read_text())
            texts.append(_sections(text, only[name]) if name in only else text)
        if spec.get("languages") and inputs.get("language"):
            texts.append(_body((self.knowledge_dir / "languages" / f"{inputs['language']}.md").read_text()))
        return "\n\n".join(texts)

    def tool_bindings(self, role: str) -> list[dict]:
        caps = self.tools.get("capabilities", {}).values()
        return [b for cap in caps if role in cap.get("roles", []) for b in cap.get("crewai") or []]
