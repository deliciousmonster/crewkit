"""Reading one SDLC document: front matter, `##` sections, `###` id entries, tables and section hashes."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

FRONT = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
COMMENT = re.compile(r"<!--.*?-->", re.S)
SECTION = re.compile(r"(?m)^## +(.+?)\s*$")
ENTRY = re.compile(r"(?m)^### +((?:REQ|Q|CR|FB|TASK)-\d+)\b *(.*)$")
FIELD = re.compile(r"(?m)^- +([A-Za-z][\w ]*?):[ \t]*(.*)$")
REQ_ID = re.compile(r"\bREQ-\d+\b")
CELL = re.compile(r"(?<!\\)\|")
SIGNOFF_HEAD = "| Rev | Date | Approver | Kind | Words | Hash |\n|---|---|---|---|---|---|"


def digest(text: str) -> str:
    """sha256 of the text with blank lines and trailing spaces dropped, first 12 hex digits."""
    norm = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


def as_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except ValueError:
        return None


@dataclass
class Entry:
    id: str
    title: str
    fields: dict[str, str]
    text: str  # the whole entry, heading included


@dataclass
class Doc:
    path: Path
    text: str
    meta: dict
    body: str  # without front matter or HTML comments
    error: str = ""  # why the file could not be read as a document

    @classmethod
    def read(cls, path: Path) -> Doc | None:
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return cls(path, "", {}, "", "not UTF-8 text")
        m = FRONT.match(text)
        body = COMMENT.sub("", text[m.end():] if m else text)
        try:
            meta = (yaml.safe_load(m.group(1)) or {}) if m else {}
        except yaml.YAMLError:
            return cls(path, text, {}, body, "front matter is not valid YAML")
        if not isinstance(meta, dict):
            return cls(path, text, {}, body, "front matter is not a mapping")
        return cls(path, text, meta, body)

    def status(self) -> str:
        return str(self.meta.get("status") or "")

    def headings(self) -> list[str]:
        return SECTION.findall(self.body)

    def section(self, title: str) -> str | None:
        """The text under `## <title>` (matched by prefix, any case) up to the next `##`, or None."""
        for m in SECTION.finditer(self.body):
            if m.group(1).lower().startswith(title.lower()):
                nxt = SECTION.search(self.body, m.end())
                return self.body[m.end(): nxt.start() if nxt else len(self.body)]
        return None

    def without(self, title: str) -> str:
        """The body with one section cut out, for a hash that must not see its own sign-off table."""
        part = self.section(title)
        return self.body.replace(part, "") if part else self.body

    def entries(self, section: str | None = None) -> list[Entry]:
        text = self.body if section is None else (self.section(section) or "")
        found = list(ENTRY.finditer(text))
        out = []
        for i, m in enumerate(found):
            end = found[i + 1].start() if i + 1 < len(found) else len(text)
            chunk = text[m.end(): end]
            chunk = chunk[: s.start()] if (s := SECTION.search(chunk)) else chunk
            fields = {k.strip().lower(): v.strip() for k, v in FIELD.findall(chunk)}
            out.append(Entry(m.group(1), m.group(2).strip(), fields, m.group(0) + chunk))
        return out

    def table(self, section: str) -> list[dict[str, str]]:
        """Rows of the first markdown table in a section, keyed by lower-cased header."""
        rows = [line.strip() for line in (self.section(section) or "").splitlines() if line.strip().startswith("|")]
        if not rows:
            return []
        split = lambda r: [c.strip().replace("\\|", "|") for c in CELL.split(r.strip().strip("|"))]  # noqa: E731
        head = [h.lower() for h in split(rows[0])]
        return [dict(zip(head, split(r))) for r in rows[1:] if not re.fullmatch(r"\|[\s:|-]*", r)]


def set_meta(text: str, key: str, value: object) -> str:
    """The text with one front matter key set, adding the key when absent."""
    line = f"{key}: {value}"
    m = FRONT.match(text)
    block = m.group(1) if m else ""
    new, n = re.subn(rf"(?m)^{re.escape(key)}:.*$", line, block, count=1)
    return f"---\n{new if n else (block + chr(10) + line).strip()}\n---\n" + (text[m.end():] if m else text)


def add_signoff(text: str, row: str) -> str:
    """The text with a row appended to its `## Sign-off` table, which lint has already required."""
    m = next(h for h in SECTION.finditer(text) if h.group(1).lower().startswith("sign-off"))
    nxt = SECTION.search(text, m.end())
    end = nxt.start() if nxt else len(text)
    part = text[m.end(): end].rstrip("\n")
    part += "\n" + row if "|" in part else "\n\n" + SIGNOFF_HEAD + "\n" + row
    return text[: m.end()] + part + "\n" + ("\n" if nxt else "") + text[end:]
