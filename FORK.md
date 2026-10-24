# Fork notes

Personal fork of [studioigor/studioigor-skill](https://github.com/studioigor/studioigor-skill).
The agent doesn't install system software, MCP servers or unreviewed npm packages itself: it hands
you a ready-to-run command (run it in a terminal or as `! <command>` in Claude Code).

## What is changed

| Where | Change |
|---|---|
| `scripts/autopilot.py` | `--scheduler system` (launchd/cron) is disabled: `start` raises with manual setup steps. caffeinate is removed |
| `scripts/guard.py` (new) | PreToolUse hook: blocks `curl \| bash`, MCP `-s user`, brew/rustup/global installs, engine installers, shell profile writes, `npx`/`npm exec` of packages not installed in the project, `npm create`/`init <pkg>`, unpinned npx MCP servers, git/URL npm deps, unreviewed small npm packages, approving pins by the agent |
| `scripts/npm_pins.py` (new) | review and pin small npm packages; pins live in `npm-pins.json` |
| `scripts/studio.py` | `init` adds the guard hook (`.claude/settings.json`), `.npmrc` and the fork rules block (`CLAUDE.md`); gate `check:` and ENV.md `TEST`/`CAPTURE` go through the guard rules before running |

Fork code is marked `# fork` — search for it when merging upstream.

## Settings (`env` in `~/.claude/settings.json`)

| Variable | Default | Meaning |
|---|---|---|
| `STUDIOIGOR_UC` | `<skill>/user-choices` | learned preferences and the project registry, kept outside the repo |
| `STUDIOIGOR_NPM_MIN_AGE_DAYS` | `7` | `min-release-age` written to a new project's `.npmrc` |
| `STUDIOIGOR_NPM_POPULAR_WEEKLY` | `50000` | weekly downloads from which an npm package counts as popular (no pin needed) |

## npm packages

- Popular (weekly downloads ≥ threshold): installed freely, protected by `min-release-age` and `ignore-scripts`.
- Small: blocked until reviewed. The agent runs the review and shows you the result:
  ```sh
  python3 scripts/npm_pins.py review <pkg>             # card + what to check by hand + the pin command
  python3 scripts/npm_pins.py pin <pkg>@x.y.z --note "what was checked"
  python3 scripts/npm_pins.py list | unpin <pkg>
  ```
  Commit `npm-pins.json` — pins are part of the fork.
- Transitive dependencies are not checked: only `min-release-age` and `ignore-scripts` cover them.
- Popularity is cached in `~/.studioigor/npm-downloads.json` for a week.

## Existing projects

The hook, `.npmrc` and the CLAUDE.md block are added by `init`. For a project created before them,
re-run it (idempotent, keeps existing values):

```sh
python3 scripts/studio.py init <game folder> --name "<name>"
```

The hook path in `<game>/.claude/settings.json` is absolute: after moving the fork, re-run `init`
and delete the old hook entry.

## Updating from upstream

```sh
git fetch upstream
git log --oneline my..upstream/main
git diff my...upstream/main -- scripts/      # read scripts before merging, especially autopilot.py
git switch main && git merge --ff-only upstream/main && git push origin main
git switch my && git rebase main
grep -rn "# fork" scripts/                   # check the fork changes survived
```
