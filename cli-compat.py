#!/usr/bin/env python3
"""Rewrite a gcodex command line into one the installed Codex CLI accepts.

Two invocations that every agent and half the humans reach for are rejected by
`codex` 0.153.x, for reasons that have nothing to do with which model is behind
the harness. Both are recoverable without changing what the caller asked for,
so the launcher recovers them here instead of passing through a clap error.

1. `-i FILE PROMPT` loses the prompt.

   `--image` is declared variadic (`<FILE>...`), so clap keeps consuming
   positionals after it and swallows the prompt as another filename. The run
   then stops on "Reading prompt from stdin... No prompt provided via stdin."
   `--image=FILE` binds exactly one value, so each image is re-emitted in that
   form and the prompt survives. Nothing else about image support is broken:
   the gateway already converts Responses `input_image` data URLs to Gemini
   `inlineData`, verified end to end against the running gateway.

2. `review --uncommitted PROMPT` is refused outright.

   `codex review` treats its scope selectors as mutually exclusive with the
   prompt: "the argument '--uncommitted' cannot be used with '[PROMPT]'". The
   two are not redundant -- a bare `codex review PROMPT` gets *no diff at all*
   (verified: the model reports NONE when asked what changed), so dropping the
   selector would silently review nothing, and dropping the prompt would
   silently review with the stock instructions. Both are kept: the selector
   stays on the command line, and the custom instructions are appended to the
   profile's `developer_instructions` so they still reach the model.

Usage:
    cli-compat.py --config <profile.toml> [--instructions <override>] -- <args...>

`--instructions` is the `developer_instructions=<json>` override the launcher
has already computed for the skill keep-list, if any; it is the base this
appends to, so the keep-list is never clobbered.

Writes NUL-separated fields to stdout: the first is the `-c` override value
(empty when there is nothing to add), the rest are the rewritten arguments.
Exits non-zero without writing anything when the line needs no rewriting, so
the caller can keep the arguments it already has.
"""
import json
import os
import sys

# Review's own value-taking options, from `codex review --help`. A token
# following one of these is that option's value, never the prompt.
REVIEW_VALUE_FLAGS = {"-c", "--config", "--enable", "--disable",
                      "--base", "--commit", "--title"}

# The selectors that decide which diff the review sees. Any one of them makes
# a positional prompt a hard parse error.
REVIEW_SCOPE_FLAGS = {"--uncommitted", "--base", "--commit"}

# Global options that take a value, so a `review` appearing right after one is
# that option's value rather than the subcommand.
GLOBAL_VALUE_FLAGS = {"-c", "--config", "--enable", "--disable", "--remote",
                      "--remote-auth-token-env", "-i", "--image", "-m", "--model",
                      "--local-provider", "-p", "--profile", "-s", "--sandbox",
                      "-C", "--cd", "--add-dir", "-a", "--ask-for-approval"}

HEADER = (
    "# Review instructions\n\n"
    "This review was started with both a scope selector and custom instructions. "
    "The Codex CLI cannot carry both on one command line, so the selector stayed "
    "there and the instructions are here. They are the user's own words and take "
    "precedence over the stock review prompt:"
)


def normalize_images(args):
    """Re-emit `-i A B` as `--image=A --image=B` so a prompt is not eaten.

    The first value after the flag is always taken, even when it does not
    exist, so a typo still surfaces as Codex's own missing-file error rather
    than as a missing prompt. Later values are taken only while they name
    something readable, which is where a prompt would otherwise be consumed.
    """
    out, changed, index = [], False, 0
    while index < len(args):
        argument = args[index]
        if argument == "--":
            out.extend(args[index:])
            break
        if argument not in ("-i", "--image"):
            out.append(argument)
            index += 1
            continue
        index += 1
        taken = 0
        while index < len(args):
            value = args[index]
            if value == "--" or (value.startswith("-") and value != "-"):
                break
            if taken and not os.path.exists(value):
                break
            out.append("--image=" + value)
            changed = True
            taken += 1
            index += 1
        if not taken:
            # Nothing to bind: hand the bare flag back and let Codex complain.
            out.append(argument)
    return out, changed


def find_subcommand(args):
    """Index of the first token that is a subcommand, or None."""
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--":
            return None
        if argument in GLOBAL_VALUE_FLAGS:
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return index
    return None


def split_review(args, start):
    """Locate a scope selector and a positional prompt after `review`.

    Returns (prompt_index, scope_flag) with either element None when absent.
    """
    prompt_index, scope = None, None
    index = start + 1
    while index < len(args):
        argument = args[index]
        if argument == "--":
            index += 1
            if index < len(args) and prompt_index is None:
                prompt_index = index
            break
        if argument in REVIEW_SCOPE_FLAGS:
            scope = scope or argument
        elif argument.split("=", 1)[0] in REVIEW_SCOPE_FLAGS and "=" in argument:
            scope = scope or argument.split("=", 1)[0]
        if argument in REVIEW_VALUE_FLAGS:
            index += 2
            continue
        if argument.startswith("-") and argument != "-":
            index += 1
            continue
        if prompt_index is None:
            prompt_index = index
        index += 1
    return prompt_index, scope


def base_instructions(config_path, precomputed):
    """The developer_instructions this appends to, or None if unreadable.

    The launcher's skill keep-list override wins when present, because it was
    already built from the profile's own value; otherwise the profile is read
    directly. None means "do not emit an override at all" -- replacing the
    profile's guidance with a review note would be a silent regression.
    """
    if precomputed:
        key, sep, value = precomputed.partition("=")
        if sep and key.strip() == "developer_instructions":
            try:
                return json.loads(value)
            except ValueError:
                return None
        return None
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


def split_arguments(argv):
    config_path, instructions, rest = None, None, []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            rest = argv[index + 1:]
            break
        if argument == "--config":
            index += 1
            config_path = argv[index] if index < len(argv) else None
        elif argument.startswith("--config="):
            config_path = argument.split("=", 1)[1]
        elif argument == "--instructions":
            index += 1
            instructions = argv[index] if index < len(argv) else None
        elif argument.startswith("--instructions="):
            instructions = argument.split("=", 1)[1]
        index += 1
    return config_path, instructions, rest


def main():
    config_path, instructions, args = split_arguments(sys.argv[1:])
    override = ""
    args, changed = normalize_images(args)

    subcommand = find_subcommand(args)
    if subcommand is not None and args[subcommand] == "review":
        prompt_index, scope = split_review(args, subcommand)
        if prompt_index is not None and scope is not None:
            prompt = args[prompt_index]
            if prompt == "-":
                prompt = sys.stdin.read()
            prompt = prompt.strip()
            base = base_instructions(config_path, instructions)
            if prompt and base is not None:
                block = HEADER + "\n\n" + prompt + "\n"
                combined = (base.rstrip("\n") + "\n\n" + block) if base.strip() else block
                # json.dumps escapes exactly what a TOML basic string needs.
                override = "developer_instructions=" + json.dumps(combined)
                args = args[:prompt_index] + args[prompt_index + 1:]
                # A `--` left with nothing after it is harmless, but dropping
                # it keeps the forwarded line identical to a hand-typed one.
                if args and args[-1] == "--":
                    args = args[:-1]
                changed = True
                print("gcodex: '%s' cannot be combined with review instructions; "
                      "kept the scope and moved the instructions into the prompt."
                      % scope, file=sys.stderr)

    if not changed:
        return 1
    sys.stdout.write("\0".join([override] + args) + "\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
