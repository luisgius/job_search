"""Lint the repo's agent skills (.agents/skills/*/SKILL.md) against the code.

A skill is read by an agent that trusts it. A renamed function or a moved doc
leaves the skill pointing at nothing, and nothing else fails. This pins:

- frontmatter parses, `name` equals the folder name, `description` is set;
- relative markdown links resolve to files inside the repo;
- every code symbol the skill names — `symbol:NNN–NNN` evidence pointers and
  the words of a `graft ask "..."` query — is a def/class in src/, and in the
  linked file when the pointer follows a link to a .py file.

Line ranges are deliberately NOT asserted: they drift with every edit and
each skill tells the reader to re-resolve them through Graft. The symbol name
is the stable part.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md"))

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# A markdown link, or a `symbol:start–end` evidence pointer (en dash or hyphen).
LINK_OR_POINTER = re.compile(
    r"\[[^\]]*\]\((?P<target>[^)\s]+)\)"
    r"|`(?P<symbol>[A-Za-z_]\w*):(?P<start>\d+)[–-](?P<end>\d+)`"
)
GRAFT_ASK = re.compile(r'graft ask "([^"]+)"')


class Pointer(NamedTuple):
    symbol: str
    start: int
    end: int
    file: str | None  # repo-relative .py file of the preceding link, if any


def _ids(paths: list[Path]) -> list[str]:
    return [p.parent.name for p in paths]


def _frontmatter(skill: Path) -> dict[str, Any]:
    match = FRONTMATTER.match(skill.read_text(encoding="utf-8"))
    assert match, f"{skill.parent.name}: SKILL.md must open with a --- frontmatter block"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict), f"{skill.parent.name}: frontmatter is not a mapping"
    return data


def _is_relative(target: str) -> bool:
    return not re.match(r"^[a-z][a-z0-9+.-]*:", target) and not target.startswith("#")


def pointers(skill: Path) -> list[Pointer]:
    """Each `symbol:NNN–NNN`, tied to the .py link before it on the same line."""
    found = []
    for line in skill.read_text(encoding="utf-8").splitlines():
        current = None
        for m in LINK_OR_POINTER.finditer(line):
            if m.group("target"):
                target = m.group("target").split("#", 1)[0]
                resolved = (skill.parent / target).resolve()
                if _is_relative(target) and target.endswith(".py") and resolved.is_relative_to(ROOT):
                    current = resolved.relative_to(ROOT).as_posix()
                else:
                    current = None
            else:
                found.append(
                    Pointer(m.group("symbol"), int(m.group("start")), int(m.group("end")), current)
                )
    return found


def graft_symbols(skill: Path) -> list[str]:
    """Identifiers in `graft ask "..."` queries; `<placeholder>` queries are skipped."""
    words = []
    for query in GRAFT_ASK.findall(skill.read_text(encoding="utf-8")):
        if "<" not in query:
            words += [w for w in query.split() if w.isidentifier()]
    return words


@lru_cache(maxsize=1)
def definitions() -> dict[str, list[tuple[str, int, int]]]:
    """name -> [(repo-relative file, first line, last line)] for every def/class in src/."""
    index: dict[str, list[tuple[str, int, int]]] = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                index.setdefault(node.name, []).append((rel, node.lineno, node.end_lineno or 0))
    return index


def test_skills_exist() -> None:
    assert SKILLS, "no .agents/skills/*/SKILL.md found"


@pytest.mark.parametrize("skill", SKILLS, ids=_ids(SKILLS))
def test_frontmatter_names_the_skill(skill: Path) -> None:
    meta = _frontmatter(skill)
    folder = skill.parent.name
    assert meta.get("name") == folder, f"name {meta.get('name')!r} != folder {folder!r}"
    assert NAME.match(folder) and len(folder) <= 64, f"{folder}: not a valid skill name"
    description = meta.get("description")
    assert isinstance(description, str) and description.strip(), f"{folder}: empty description"
    assert len(description) <= 1024, f"{folder}: description over 1024 characters"


@pytest.mark.parametrize("skill", SKILLS, ids=_ids(SKILLS))
def test_relative_links_resolve(skill: Path) -> None:
    text = skill.read_text(encoding="utf-8")
    targets = [m.group("target") for m in LINK_OR_POINTER.finditer(text) if m.group("target")]
    for target in targets:
        if not _is_relative(target):
            continue
        resolved = (skill.parent / target.split("#", 1)[0]).resolve()
        assert resolved.is_relative_to(ROOT), f"{skill.parent.name}: {target} leaves the repo"
        assert resolved.exists(), f"{skill.parent.name}: broken link {target}"


@pytest.mark.parametrize("skill", SKILLS, ids=_ids(SKILLS))
def test_evidence_pointers_name_real_symbols(skill: Path) -> None:
    index = definitions()
    for p in pointers(skill):
        files = {f for f, _, _ in index.get(p.symbol, [])}
        assert files, f"{skill.parent.name}: `{p.symbol}` is not a def/class in src/"
        if p.file is not None:
            assert p.file in files, (
                f"{skill.parent.name}: `{p.symbol}` follows a link to {p.file} "
                f"but is defined in {sorted(files)}"
            )


@pytest.mark.parametrize("skill", SKILLS, ids=_ids(SKILLS))
def test_graft_queries_name_real_symbols(skill: Path) -> None:
    index = definitions()
    for word in graft_symbols(skill):
        assert word in index, f"{skill.parent.name}: graft ask names `{word}`, not in src/"
