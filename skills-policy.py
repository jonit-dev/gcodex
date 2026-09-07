#!/usr/bin/env python3
"""Emit a Codex `skills.config` override that keeps only the skills you use.

Codex ships every installed skill's name, description and path in the developer
message on *every* model call. With a large library that is the single largest
item in the request -- measured here at 81 KB of 114 KB, resent 12-15 times per
task against a per-request subscription quota.

`skills.config` is a per-skill override list layered on top of "everything
enabled", not an allowlist: naming one skill with enabled=true leaves the other
204 on. So keeping a subset means naming every skill you do *not* want. This
script does that from a keep-list, so the list stays readable and the disable
list is regenerated whenever the installed skills change.

Usage:
    skills-policy.py <keep-list-file> [skill-root ...]

Prints one `-c` value for codex, or nothing when the keep-list is absent or
empty -- in which case the caller should fall back to dropping skills entirely.
The keep-list is one skill name per line; blank lines and `#` comments ignored.
"""
import os
from pathlib import Path
import sys


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
    names = set()
    for root in roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            # A skill is a directory holding SKILL.md. Skip dotted internals
            # such as .sys so they are never named in the override.
            if entry.name.startswith('.') or not entry.is_dir():
                continue
            if (entry / "SKILL.md").is_file():
                names.add(entry.name)
    return names


def read_keep(path):
    keep = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return keep
    for line in lines:
        line = line.split('#', 1)[0].strip()
        if line:
            keep.append(line)
    return keep


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    keep = set(read_keep(sys.argv[1]))
    if not keep:
        return 0
    names = installed(skill_roots(sys.argv[2:]))
    if not names:
        return 0
    disabled = sorted(n for n in names if n not in keep)
    if not disabled:
        return 0
    # Only names matching this shape are emitted, so nothing can break out of
    # the TOML string and inject another config key.
    safe = [n for n in disabled if n.replace('-', '').replace('_', '').replace('.', '').isalnum()]
    entries = ",".join('{name="%s",enabled=false}' % n for n in safe)
    print("skills.config=[%s]" % entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
