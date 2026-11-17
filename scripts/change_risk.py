#!/usr/bin/env python3
"""Classify a pull request diff as low-risk site content or human review.

Low risk is markdown and ordinary site images, plus title, description, and
tagline edits in _config.yml. Everything else fails closed. A secret-shaped
value added in a text diff is high risk even inside an otherwise safe file.
"""

from __future__ import annotations

import re

SAFE_IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
SAFE_TEXT = {".md", ".markdown"}
SAFE_CONFIG_KEYS = {"title", "description", "tagline"}
DEPENDENCY_FILES = {
    "gemfile",
    "gemfile.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements.txt",
    "pipfile",
    "pipfile.lock",
    "pyproject.toml",
    "poetry.lock",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
    "composer.json",
    "composer.lock",
}
INFRA_PREFIXES = (".github/", "scripts/", "tests/")
TOKEN_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"|\bcrsr_[A-Za-z0-9]{8,}"
    r"|\bcursor_[A-Za-z0-9]{20,}"
    r"|\bghp_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
)


def classify_files(files: list[dict], truncated: bool = False) -> dict:
    if truncated:
        return {"risk": "high", "reasons": ["file_list_truncated"]}
    if not files:
        return {"risk": "high", "reasons": ["empty_change"]}
    reasons: list[str] = []
    for item in files:
        reasons.extend(reasons_for_file(item))
    if reasons:
        return {"risk": "high", "reasons": reasons}
    return {"risk": "low", "reasons": []}


def reasons_for_file(item: dict) -> list[str]:
    filename = str(item.get("filename") or "")
    previous = str(item.get("previous_filename") or "")
    reasons: list[str] = []
    saw_config = False
    for path in (filename, previous):
        if not path:
            continue
        kind = path_kind(path)
        if kind == "safe":
            continue
        if kind == "config":
            saw_config = True
            continue
        reasons.append(f"{kind}:{path}")
    if saw_config and not config_patch_is_safe(item.get("patch")):
        reasons.append(f"unsafe_config:{filename or '_config.yml'}")
    patch = item.get("patch")
    if path_kind(filename) == "safe" and _extension(filename) in SAFE_TEXT and item.get("status") != "removed":
        if not isinstance(patch, str):
            reasons.append(f"unreadable_diff:{filename}")
        elif added_secret(patch):
            reasons.append(f"secret_material:{filename}")
    elif isinstance(patch, str) and added_secret(patch):
        reasons.append(f"secret_material:{filename or previous}")
    return reasons


def path_kind(path: str) -> str:
    normalised = path.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    normalised = normalised.lstrip("/")
    name = normalised.rsplit("/", 1)[-1]
    lower_name = name.lower()
    folded = normalised.lower()
    if folded.startswith(INFRA_PREFIXES) or lower_name in {"cname", "codeowners", "dockerfile"}:
        return "infrastructure"
    if lower_name.startswith("docker-compose") or folded.endswith((".tf", ".tfvars")):
        return "infrastructure"
    if lower_name in DEPENDENCY_FILES or folded.endswith(".gemspec"):
        return "dependency"
    if lower_name.startswith(".env") or folded.endswith((".pem", ".key", ".p12", ".pfx")):
        return "secret_material"
    if lower_name == "_config.yml":
        return "config"
    if _extension(name) in SAFE_TEXT or _extension(name) in SAFE_IMAGES:
        return "safe"
    return "unknown_path"


def config_patch_is_safe(patch: object) -> bool:
    if not isinstance(patch, str) or not patch.strip():
        return False
    changed = False
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "@@")) or not line.startswith(("+", "-")):
            continue
        content = line[1:]
        if content.startswith((" ", "\t")) or not content.strip() or content.lstrip().startswith("#"):
            return False
        if ":" not in content:
            return False
        key = content.split(":", 1)[0].strip()
        if key not in SAFE_CONFIG_KEYS:
            return False
        changed = True
    return changed


def added_secret(patch: str) -> bool:
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++") and TOKEN_RE.search(line):
            return True
    return False


def _extension(name: str) -> str:
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()
