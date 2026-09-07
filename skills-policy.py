#!/usr/bin/env python3
"""Emit a Codex `developer_instructions` override that advertises kept skills.

Codex ships every installed skill's name, description and path in the developer
message on *every* model call. With a large library that is the single largest
item in the request -- measured here at 81 KB of 114 KB, resent 12-15 times per
task against a per-request subscription quota.

The obvious trim is `skills.config`, a per-skill override layered on top of
"everything enabled": naming every skill you do *not* want with enabled=false
leaves only the keep-list in the prompt. It also breaks skill tagging. A
disabled skill is gone from the `$` picker in the composer, so the user cannot
reach it by hand either -- and reaching for one deliberately is the cheapest
way to use a skill, because it costs nothing until it is used.

So the trim runs the other way round: `skills.include_instructions=false` drops
the whole catalog from the prompt while leaving every skill installed, enabled
and taggable, and this script re-advertises just the keep-list by appending it
to the profile's own `developer_instructions`. Same prompt cost as the old
disable list, and all 200-odd skills stay one `$` away.

Usage:
    skills-policy.py <keep-list-file> --config <profile.toml> [skill-root ...]

Prints one `-c` value for codex, or nothing when the keep-list is absent or
empty, or when the profile's own instructions cannot be read -- in which case
the caller should fall back to dropping the catalog and nothing else. The
keep-list is one skill name per line; blank lines and `#` comments ignored.
"""
import json
import os
from pathlib import Path
import sys

# Long enough to tell two skills apart, short enough that a keep-list of a few
# dozen entries stays a rounding error next to the catalog it replaces.
DESCRIPTION_LIMIT = 240

HEADER = (
    "Skills available in this session. The full catalog is omitted to keep each "
    "request small; these are the ones kept for you. To use one, read its SKILL.md "
    "and follow it. Other skills are still installed and can be named by the user."
)


def skill_roots(extra):
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    roots = [codex_home / "skills", Path.home() / ".agents" / "skills"]
    roots.extend(Path(p) for p in extra)
    seen, out = set(), []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def installed(roots):
    """Map skill name -> SKILL.md path. Earlier roots win, matching Codex."""
    found = {}
    for root in roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            # A skill is a directory holding SKILL.md. Skip dotted internals
            # such as .sys so they are never advertised.
            if entry.name.startswith('.') or not entry.is_dir():
                continue
            manifest = entry / "SKILL.md"
            if manifest.is_file() and entry.name not in found:
                found[entry.name] = manifest
    return found


def read_keep(path):
    keep = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return keep
    for line in lines:
        line = line.split('#', 1)[0].strip()
        if line and line not in keep:
            keep.append(line)
    return keep


def description(manifest):
    """The `description:` line from the SKILL.md front matter, if there is one."""
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() == "description":
            value = value.strip().strip('"').strip("'").strip()
            return value[:DESCRIPTION_LIMIT]
    return ""


def base_instructions(config_path):
    """The profile's own developer_instructions, which this override replaces.

    Returned as None when it cannot be read: emitting a replacement that drops
    the profile's guidance would be a silent regression, so the caller is
    expected to skip the override entirely instead.
    """
    if not config_path:
        return None
    try:
        import tomllib
    except ImportError:
        return None
    try:
        with open(config_path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return None
    value = data.get("developer_instructions")
    if value is None:
        return ""
    return value if isinstance(value, str) else None


def catalog(keep, skills):
    lines = []
    for name in keep:
        manifest = skills.get(name)
        if manifest is None:
            continue
        summary = description(manifest)
        lines.append(f"- {name}: {summary} [{manifest}]" if summary else f"- {name}: [{manifest}]")
    return lines


def split_arguments(argv):
    keep_path, config_path, roots = None, None, []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--config":
            index += 1
            if index >= len(argv):
                return None, None, None
            config_path = argv[index]
        elif argument.startswith("--config="):
            config_path = argument.split("=", 1)[1]
        elif keep_path is None:
            keep_path = argument
        else:
            roots.append(argument)
        index += 1
    return keep_path, config_path, roots


def main():
    keep_path, config_path, roots = split_arguments(sys.argv[1:])
    if not keep_path:
        print(__doc__, file=sys.stderr)
        return 2
    keep = read_keep(keep_path)
    if not keep:
        return 0
    entries = catalog(keep, installed(skill_roots(roots)))
    if not entries:
        return 0
    base = base_instructions(config_path)
    if base is None:
        return 0
    block = HEADER + "\n\n" + "\n".join(entries) + "\n"
    combined = (base.rstrip("\n") + "\n\n" + block) if base.strip() else block
    # json.dumps escapes exactly what a TOML basic string needs: quotes,
    # backslashes, newlines and every control or non-ASCII character.
    print("developer_instructions=" + json.dumps(combined))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
