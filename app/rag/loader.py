"""Load policy markdown files and their YAML frontmatter.

Frontmatter carries the machine-readable `rules` block that the deterministic
eligibility engine reads; the markdown body is what the model sees. Keeping both
in one file means a policy change updates the prose and the enforced rule
together, instead of letting them drift apart.
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)

REQUIRED_FIELDS = ("slug", "title", "category")


class PolicyParseError(ValueError):
    """A policy file is missing frontmatter or a required field."""


@dataclass
class PolicyFile:
    slug: str
    title: str
    category: str
    version: str
    body: str
    source_path: str
    rules: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """Hash of the body plus rules, so ingestion can skip unchanged files."""
        payload = self.body + yaml.safe_dump(self.rules, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_policy(text: str, source_path: str) -> PolicyFile:
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise PolicyParseError(f"{source_path}: missing YAML frontmatter block")

    raw_meta, body = match.groups()
    meta = yaml.safe_load(raw_meta) or {}
    if not isinstance(meta, dict):
        raise PolicyParseError(f"{source_path}: frontmatter must be a mapping")

    missing = [f for f in REQUIRED_FIELDS if not meta.get(f)]
    if missing:
        raise PolicyParseError(f"{source_path}: missing frontmatter field(s): {', '.join(missing)}")

    return PolicyFile(
        slug=str(meta["slug"]),
        title=str(meta["title"]),
        category=str(meta["category"]),
        version=str(meta.get("version", "1")),
        body=body.strip(),
        source_path=source_path,
        rules=meta.get("rules") or {},
    )


def load_policies(directory: Path) -> list[PolicyFile]:
    """Parse every `.md` file in `directory`, sorted for deterministic ingestion."""
    if not directory.exists():
        raise FileNotFoundError(f"Policy directory not found: {directory}")

    policies: list[PolicyFile] = []
    for path in sorted(directory.glob("*.md")):
        policies.append(parse_policy(path.read_text(encoding="utf-8"), path.name))

    if not policies:
        raise PolicyParseError(f"No policy documents found in {directory}")

    slugs = [p.slug for p in policies]
    duplicates = {s for s in slugs if slugs.count(s) > 1}
    if duplicates:
        raise PolicyParseError(f"Duplicate policy slug(s): {', '.join(sorted(duplicates))}")

    return policies
