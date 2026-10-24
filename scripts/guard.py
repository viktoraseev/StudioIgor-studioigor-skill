#!/usr/bin/env python3
"""guard — fork-only PreToolUse hook: the agent doesn't install system software itself.

Blocks Bash commands that pipe a download into a shell, add a user-scope MCP server, install
system-wide software (brew, rustup, global npm/pipx/uv/cargo/gem, engine installers) or write
to shell profiles, plus Edit/Write on shell profiles. JS: blocks running packages straight
from the registry (npx/npm exec/bunx/pnpm dlx/yarn dlx of a package not installed in the
project, npm create/init <pkg>) and MCP servers launched via npx without a pinned version.
The agent gets the reason back and must hand the exact command to the user to run manually.

studio.py init registers it in <game>/.claude/settings.json, so it also covers autopilot ticks.
Reads the hook JSON on stdin; stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

sys.dont_write_bytecode = True

PROFILE = r"\.(?:zshrc|zprofile|zshenv|zlogin|bashrc|bash_profile|bash_login|profile)\b"
RULES = [
    ("a download piped into a shell (curl | bash)",
     r"\b(?:curl|wget)\b[^;&]*?\|\s*(?:sudo\s+)?(?:env\s+)?(?:\w+=\S*\s+)*(?:ba|z|da|k)?sh\b"
     r"|\b(?:ba|z)?sh\s+(?:-c\s+)?[\"']?(?:\$\(|<\()\s*(?:curl|wget)\b"),
    ("a user-scope MCP server (claude mcp add -s user)",
     r"\bclaude\s+mcp\s+add(?:-json|-from-claude-desktop)?\b.*(?:-s|--scope)[=\s]+user\b"),
    ("a system-wide install (brew)", r"\bbrew\s+(?:install|reinstall|upgrade|tap|bundle)\b"),
    ("a Rust toolchain install (rustup)",
     r"sh\.rustup\.rs|\brustup-init\b|\brustup\s+(?:install|update|toolchain\s+install|target\s+add|component\s+add)\b"),
    ("a global package install",
     r"\bnpm\s+(?:i|install|add)\b[^;&|]*\s(?:-g|--global)\b|\bnpm\s+(?:-g|--global)\s+(?:i|install|add)\b"
     r"|\bpnpm\s+(?:add|i|install)\b[^;&|]*\s(?:-g|--global)\b|\byarn\s+global\s+add\b"
     r"|\bpipx\s+install\b|\buv\s+tool\s+install\b|\bcargo\s+install\b|\bgem\s+install\b|\bgo\s+install\b"),
    ("a system or engine installer",
     r"\bxcode-select\s+--install\b|\bsoftwareupdate\b|\bmas\s+install\b|\binstaller\s+-pkg\b"
     r"|\bunity\s+install(?:-modules)?\b|\bsdkmanager\b"),
    ("a write to a shell profile",
     rf"(?:>>?|\btee\b|\bsed\s+-i|\bperl\s+-p?i)[^;&|]*{PROFILE}"
     rf"|(?:\bcp|\bmv|\bln)\b[^;&|]*{PROFILE}\s*(?:$|[;&|])"),
    ("approving npm packages (npm_pins.py pin/unpin is for the user only)",
     r"\bnpm_pins\.py\s+(?:pin|unpin)\b"
     r"|(?:>>?|\btee\b|\bsed\s+-i|\bperl\s+-p?i|\bcp\b|\bmv\b|\brm\b|\bln\b)[^;&|]*npm-pins\.json"),
]
RULES = [(what, re.compile(rx)) for what, rx in RULES]
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MCP_FILES = {".mcp.json", ".claude.json"}

# --------------------------------------------------------------------------- JS
SEPS = {";", "&&", "||", "|", "&", "|&", "(", ")", "\n"}
LOCAL_ONLY = {"--no", "--no-install", "--offline"}
RUNNERS = re.compile(r"\b(?:npx|bunx|pnpx|(?:pnpm|yarn)\s+dlx|bun\s+x)\s+((?:-\S+\s+)*)([^\s\"',\]]+)")
PINNED = re.compile(r"^(?:@[^/@\s]+/)?[^@\s]+@v?\d[^\s]*$")
FROM_REGISTRY = ("running a package straight from the npm registry",
                 "Install it into the project first (npm i -D <pkg>, then `npx --no <bin>`), "
                 "or hand the command to the user")
CREATE = ("an npm create/init scaffolder (downloads and runs create-<pkg>)",
          "Hand the command to the user to run manually")
MCP_UNPINNED = ("an MCP server launched via npx without a pinned version",
                "Pin an exact version that is at least the project's min-release-age days old: <pkg>@x.y.z")
SMALL = "an unreviewed small npm package"
NON_REGISTRY = ("an npm dependency from outside the registry (git/URL): it skips min-release-age and the pins",
                "Hand the command to the user to review and run manually")
INSTALL = {"i", "install", "add", "ci", "it", "install-test", "update", "up", "upgrade"}
FLAGS_WITH_VALUE = {"--prefix", "-C", "--workspace", "-w", "--tag", "--save-prefix", "--omit", "--include"}
_pins = None


def pins():
    """npm_pins.py from the same folder (python3 -I doesn't put the script folder on sys.path)."""
    global _pins
    if _pins is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("studioigor_npm_pins",
                                                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                   "npm_pins.py"))
        _pins = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_pins)
    return _pins


def non_registry(spec: str) -> bool:
    return bool(re.match(r"(?:git\+|git:|github:|gitlab:|bitbucket:|gist:|https?:)", spec)
                or re.fullmatch(r"[\w.-]+/[\w.-]+(?:#.*)?", spec))


def install_blocked(args: list[str], cwd: str | None) -> tuple[str, str] | None:
    """npm install/ci/update: every direct package must be popular or pinned (npm_pins.py)."""
    specs, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in FLAGS_WITH_VALUE:
            skip = True
        elif not a.startswith("-"):
            specs.append(a)
    deps = []
    for s in specs:
        if s.startswith((".", "/", "~", "file:")):
            continue
        if non_registry(s):
            return NON_REGISTRY
        deps.append(pins().split_spec(s))
    if not specs and cwd:
        try:
            pj = json.load(open(os.path.join(cwd, "package.json"), encoding="utf-8"))
        except (OSError, ValueError):
            pj = {}
        for sect in ("dependencies", "devDependencies", "optionalDependencies"):
            for name, ver in (pj.get(sect) or {}).items():
                ver = str(ver)
                if ver.startswith(("file:", "link:", "workspace:", ".", "/")):
                    continue
                if non_registry(ver):
                    return NON_REGISTRY
                deps.append((name, ver))
    for name, ver in deps:
        why = pins().check(name, ver)
        if why:
            return SMALL, why
    return None


def simple_commands(cmd: str) -> list[list[str]]:
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        toks = cmd.split()
    out = [[]]
    for t in toks:
        if t in SEPS:
            out.append([])
        else:
            out[-1].append(t)
    res = []
    for c in out:
        while c and (re.fullmatch(r"\w+=.*", c[0]) or c[0] in ("sudo", "exec", "command", "env", "time", "nohup")):
            c = c[1:]
        if c:
            res.append(c)
    return res


def local_bin(pkg: str, cwd: str | None) -> bool:
    return bool(cwd) and "@" not in pkg and "/" not in pkg and os.path.exists(os.path.join(cwd, "node_modules", ".bin", pkg))


def runner_blocked(args: list[str], cwd: str | None) -> tuple[str, str] | None:
    """args after npx / npm exec / bunx / dlx: flags, then the package."""
    flags, rest = [], []
    for i, a in enumerate(args):
        if a == "--":
            rest = args[i + 1:]
            break
        if a.startswith("-"):
            flags.append(a)
            continue
        rest = args[i:]
        break
    if LOCAL_ONLY & set(flags):
        return None
    if any(f in ("-p", "--package") or f.startswith("--package=") for f in flags):
        return FROM_REGISTRY
    return None if rest and local_bin(rest[0], cwd) else FROM_REGISTRY


def mcp_blocked(text: str) -> tuple[str, str] | None:
    for m in RUNNERS.finditer(text):
        if LOCAL_ONLY & set(m.group(1).split()):
            continue
        if not PINNED.match(m.group(2)):
            return MCP_UNPINNED
        why = pins().check(*pins().split_spec(m.group(2)))
        if why:
            return SMALL, why
    return None


def js_blocked(cmd: str, cwd: str | None) -> tuple[str, str] | None:
    for c in simple_commands(cmd):
        name = os.path.basename(c[0])
        if name == "claude" and len(c) > 2 and c[1] == "mcp" and c[2].startswith("add"):
            r = mcp_blocked(" ".join(c[3:]))
            if r:
                return r
            continue
        sub = next((a for a in c[1:] if not a.startswith("-")), "")
        rest = c[c.index(sub) + 1:] if sub else []
        if name in ("npx", "bunx", "pnpx"):
            r = runner_blocked(c[1:], cwd)
        elif name == "npm" and sub in ("exec", "x"):
            r = runner_blocked(rest, cwd)
        elif name == "npm" and sub in ("create", "init", "innit") and any(not a.startswith("-") for a in rest):
            r = CREATE
        elif name == "npm" and sub in INSTALL:
            r = install_blocked(rest, cwd)
        elif name in ("pnpm", "yarn") and sub == "dlx":
            r = runner_blocked(rest, cwd)
        elif name == "bun" and sub == "x":
            r = runner_blocked(rest, cwd)
        elif name in ("pnpm", "yarn", "bun") and sub == "create":
            r = CREATE
        else:
            r = None
        if r:
            return r
    return None


# --------------------------------------------------------------------------- API
def blocked(cmd: str, cwd: str | None = None) -> str | None:
    """What a shell command would do that the guard forbids (with a hint), or None. Also used by studio.py."""
    for what, rx in RULES:
        if rx.search(cmd):
            return what
    r = js_blocked(cmd, cwd)
    return f"{r[0]}. {r[1]}" if r else None


def deny(what: str, attempt: str) -> int:
    reason = (f"[studioigor fork guard] Blocked {what}: {attempt}\n"
              "Don't install it yourself and don't work around this block. Show the user the exact command to run "
              "manually (in a terminal, or `! <command>` in Claude Code), then continue with work that doesn't need "
              "it. In autopilot: mark the gate BLOCKED with this reason.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    return 0


def main() -> int:
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return 0
    tool, inp, cwd = ev.get("tool_name", ""), ev.get("tool_input") or {}, ev.get("cwd")
    if tool == "Bash":
        cmd = str(inp.get("command", ""))
        what = blocked(cmd, cwd)
        if what:
            return deny(what, cmd)
    elif tool in FILE_TOOLS:
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        base = os.path.basename(path)
        if re.fullmatch(PROFILE.replace(r"\b", "") + r"(?:\.local)?", base):
            return deny("a write to a shell profile", f"{tool} {path}")
        if base == "npm-pins.json":
            return deny("approving npm packages (npm-pins.json is edited by the user only)", f"{tool} {path}")
        if base in MCP_FILES:
            text = " ".join(str(inp.get(k, "")) for k in ("content", "new_string"))
            text += " ".join(str(e.get("new_string", "")) for e in inp.get("edits", []) if isinstance(e, dict))
            r = mcp_blocked(re.sub(r'["\[\]{},:]|\b(?:command|args)\b', " ", text))
            if r:
                return deny(f"{r[0]}. {r[1]}", f"{tool} {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
