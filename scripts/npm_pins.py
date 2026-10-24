#!/usr/bin/env python3
"""npm_pins — fork-only: small npm packages are installed only at a manually reviewed version.

min-release-age (.npmrc) protects popular packages: someone notices a bad release within days.
Small packages have nobody watching, so they need a human review, and the reviewed version is
pinned in <skill>/npm-pins.json. guard.py enforces it for npm install/ci and npx MCP servers:

  popular (weekly downloads >= STUDIOIGOR_NPM_POPULAR_WEEKLY, default 50000) — allowed;
  pinned — allowed only at the pinned version;
  anything else (small, or popularity unknown) — blocked until reviewed and pinned.

Only direct dependencies are checked; transitive ones rely on min-release-age and ignore-scripts.

  npm_pins.py review <pkg>[@version]   what to look at before pinning (registry data)
  npm_pins.py pin <pkg>@<version> --note "what was checked"
  npm_pins.py unpin <pkg>
  npm_pins.py list

pin/unpin are for the human: guard.py blocks the agent from running them or editing npm-pins.json.
stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
SKILL = Path(__file__).resolve().parent.parent
PINS = SKILL / "npm-pins.json"
CACHE = Path.home() / ".studioigor" / "npm-downloads.json"
CACHE_TTL = 7 * 24 * 3600
REGISTRY = "https://registry.npmjs.org/"
DOWNLOADS = "https://api.npmjs.org/downloads/point/last-week/"
INSTALL_SCRIPTS = ("preinstall", "install", "postinstall", "prepare")


def popular_threshold() -> int:
    return int(os.environ.get("STUDIOIGOR_NPM_POPULAR_WEEKLY", "50000"))


def min_age_days() -> int:
    return int(os.environ.get("STUDIOIGOR_NPM_MIN_AGE_DAYS", "7"))


def get_json(url: str, timeout: float = 8) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "studioigor-npm-pins"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def quote(name: str) -> str:
    return urllib.parse.quote(name, safe="@")


def load_pins() -> dict:
    try:
        return json.loads(PINS.read_text(encoding="utf-8")).get("pins", {})
    except (OSError, ValueError):
        return {}


def save_pins(pins: dict) -> None:
    PINS.write_text(json.dumps({"pins": dict(sorted(pins.items()))}, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def weekly_downloads(name: str) -> int | None:
    """Weekly downloads, cached for a week in ~/.studioigor; None if unknown (offline, error)."""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(name)
    if hit and time.time() - hit["at"] < CACHE_TTL:
        return hit["n"]
    try:
        n = int(get_json(DOWNLOADS + quote(name), timeout=5).get("downloads", 0))
    except Exception:
        return None
    cache[name] = {"n": n, "at": int(time.time())}
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    return n


def split_spec(spec: str) -> tuple[str, str]:
    """'@scope/pkg@1.2.3' -> ('@scope/pkg', '1.2.3'); no version -> ('pkg', '')."""
    at = spec.find("@", 1)
    return (spec, "") if at < 0 else (spec[:at], spec[at + 1:])


def check(name: str, version: str) -> str | None:
    """Why installing name@version (version: exact, range or '') is not allowed, or None."""
    pin = load_pins().get(name)
    if pin:
        if version.lstrip("=v") == pin["version"]:
            return None
        return (f"{name} is a reviewed small package pinned at {pin['version']} (npm-pins.json); "
                f"got {version or 'no version'}. Use exactly {name}@{pin['version']}")
    n = weekly_downloads(name)
    if n is not None and n >= popular_threshold():
        return None
    why = "popularity unknown (offline?)" if n is None else f"{n} weekly downloads < {popular_threshold()}"
    return (f"{name} is a small npm package ({why}) and not reviewed. Run the read-only review yourself: "
            f"python3 {SKILL}/scripts/npm_pins.py review {name} — show the user its output (the card, the manual "
            "checks and the exact `pin` command at the end). The user does the checks and runs `pin`; then retry "
            "with exactly that version. Never run pin/unpin yourself")


# --------------------------------------------------------------------------- CLI
def age_days(ts: str) -> int:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).days


def cmd_review(a) -> int:
    name, want = split_spec(a.spec)
    doc = get_json(REGISTRY + quote(name), timeout=20)
    times = {v: t for v, t in doc.get("time", {}).items() if v in doc.get("versions", {})}
    if not times:
        print(f"{name}: no versions in the registry")
        return 1
    old = [v for v in times if age_days(times[v]) >= min_age_days()]
    ver = want or (max(old, key=lambda v: times[v]) if old else max(times, key=lambda v: times[v]))
    if ver not in doc["versions"]:
        print(f"{name}@{ver}: no such version")
        return 1
    m = doc["versions"][ver]
    latest = doc.get("dist-tags", {}).get("latest", "")
    scripts = {k: v for k, v in (m.get("scripts") or {}).items() if k in INSTALL_SCRIPTS}
    repo = m.get("repository") or doc.get("repository") or {}
    repo = repo.get("url", "") if isinstance(repo, dict) else str(repo)
    deps = m.get("dependencies") or {}
    n = weekly_downloads(name)
    print(f"{name}@{ver}  (latest {latest}, published {times[ver][:10]}, {age_days(times[ver])} days ago)")
    print(f"  weekly downloads: {n if n is not None else 'unknown'} (popular from {popular_threshold()})")
    print(f"  license: {m.get('license', '?')}   versions: {len(times)}   first published: {doc.get('time', {}).get('created', '?')[:10]}")
    print(f"  maintainers: {', '.join(x.get('name', '?') for x in doc.get('maintainers', []))}")
    print(f"  publisher of this version: {(m.get('_npmUser') or {}).get('name', '?')}")
    print(f"  repository: {repo or 'NONE'}")
    print(f"  provenance: {'yes' if (m.get('dist') or {}).get('attestations') else 'no'}")
    print(f"  install scripts: {json.dumps(scripts) if scripts else 'none'}")
    print(f"  bin: {json.dumps(m.get('bin')) if m.get('bin') else 'none'}")
    print(f"  dependencies ({len(deps)}): {', '.join(f'{k}@{v}' for k, v in deps.items()) or 'none'}")
    print(f"  unpacked size: {(m.get('dist') or {}).get('unpackedSize', '?')} bytes, files: {(m.get('dist') or {}).get('fileCount', '?')}")
    print("\nCheck by hand:")
    print(f"  - the code: mkdir -p /tmp/rv && cd /tmp/rv && npm pack {name}@{ver} && tar xzf *.tgz && ls -R package")
    print(f"    (look at the bin/main entry points, network calls, child_process, eval, obfuscated code)")
    print(f"  - the tarball matches the repository tag for {ver}; recent maintainer changes")
    print(f"  - https://socket.dev/npm/package/{name}/overview/{ver}")
    print(f"\nThen: python3 {Path(__file__).resolve()} pin {name}@{ver} --note \"what was checked\"")
    return 0


def cmd_pin(a) -> int:
    name, ver = split_spec(a.spec)
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", ver):
        print("pin an exact version: <pkg>@x.y.z")
        return 1
    pins = load_pins()
    pins[name] = {"version": ver, "reviewed": date.today().isoformat(), "note": a.note}
    save_pins(pins)
    print(f"pinned {name}@{ver} in {PINS}")
    return 0


def cmd_unpin(a) -> int:
    pins = load_pins()
    if pins.pop(a.name, None) is None:
        print(f"{a.name} is not pinned")
        return 1
    save_pins(pins)
    print(f"unpinned {a.name}")
    return 0


def cmd_list(a) -> int:
    pins = load_pins()
    for k, v in pins.items():
        print(f"{k}@{v['version']}  reviewed {v['reviewed']}  {v.get('note', '')}")
    if not pins:
        print("no pins")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="reviewed versions of small npm packages")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("review")
    p.add_argument("spec")
    p = sp.add_parser("pin")
    p.add_argument("spec")
    p.add_argument("--note", required=True)
    p = sp.add_parser("unpin")
    p.add_argument("name")
    sp.add_parser("list")
    a = ap.parse_args()
    return {"review": cmd_review, "pin": cmd_pin, "unpin": cmd_unpin, "list": cmd_list}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
