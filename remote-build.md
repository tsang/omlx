# Remote oMLX.app build on GitHub — live handoff doc

**Purpose:** single source of truth for building `oMLX.app` remotely on
GitHub Actions, and for resuming this work after a chat compaction.
Keep this file current every cycle. If the conversation loses context,
read this file and continue from "Current state" + "Resume procedure".

## Repo / remotes / auth

- Local checkout: `~/Sync/devel/omlx` (git repo).
- Upstream: `origin` = https://github.com/jundot/omlx.git (do NOT push).
- Fork / CI target: `tsang` = https://github.com/tsang/omlx.git.
- `gh` CLI authenticated as account **tsang** (scopes incl. repo, workflow).
- Fork's default branch `main` carries our fork commits on top of upstream.
- `watch-actions.sh` is local-only (in `.git/info/exclude`), not committed.
- Branch `fix/admin-stats-idle-visibility` holds the admin-stats snapshot
  fix (already merged to fork main).

## Why CI builds the app here at all

Upstream does NOT build the .app in CI — release DMGs come from an
off-tree maintainer machine (`packaging/README.md:69-72`). This fork adds
`.github/workflows/build-app.yml` to build it on a hosted `macos-15`
runner and upload the `.app` as an artifact.

## The workflow: `.github/workflows/build-app.yml`

- `workflow_dispatch` inputs: `configuration` (release|debug),
  `custom_kernels` (bool → adds `--with-custom-kernel`).
- `runs-on: macos-15`, newest Xcode via `sort -V` (runner has 26.3).
- Steps: checkout → select Xcode → setup Python 3.11 →
  `pip install venvstacks` → `build.sh release` → smoke test →
  `ditto` zip + sha256 → upload artifact `oMLX-app-<sha>`.
- On failure: dumps error lines from `apps/omlx-mac/build/xcodebuild.log`
  and uploads artifact `xcodebuild-log-<sha>`.

`build.sh release` internally: resolves SPM → `xcodebuild` (Release builds
arm64 AND x86_64) → rebuilds venvstacks export when stale
(`packaging/build.py --venvstacks-only`, ~6 min, `cpython-3.11` +
`framework-mlx-base` ~1 GB) → stages `apps/omlx-mac/build/Stage/oMLX.app`
→ embeds Python + `omlx` pkg → `actool` Tahoe icon → ad-hoc sign.

## Trigger + monitor commands

```bash
cd ~/Sync/devel/omlx
git push tsang main
gh workflow run "Build macOS app" --repo tsang/omlx --ref main

# monitor (background): prints status transitions, dumps failed logs
./watch-actions.sh tsang/omlx 45

# manual peek / pull diagnostics
gh run list --repo tsang/omlx --limit 5
gh run view <run-id> --repo tsang/omlx --log-failed
gh run download <run-id> --repo tsang/omlx --name xcodebuild-log-<sha>
```

Timing: ~2 min to early failure, ~15-30 min full build.

## CURRENT STATE  (update this every cycle)

- HEAD on fork `main`: **74ff125a**
  "build: don't abort when actool emits no AppIcon.icns".
- Build **IN PROGRESS**: run **33785347549** —
  https://github.com/tsang/omlx/actions/runs/33785347549
  Monitoring in background shell id **041** (`./watch-actions.sh tsang/omlx 45`).
- CI test run (auto, push trigger) in progress: 33785355767 (expected pass).
- If run 33785347549 turns GREEN: artifact `oMLX-app-<sha>` is the product
  (zip + sha256). Update this section to GREEN, record artifact name+size.
- If it FAILS again: `gh run view 33785347549 --repo tsang/omlx --log-failed`
  (plus download `xcodebuild-log-<sha>`), add a row to the failure table
  below, fix, re-push, re-dispatch, restart the monitor.

### Failure history + fixes

| run | stage failed | root cause | fix |
|-----|--------------|-----------|-----|
| 33774786542 | Swift compile (arm64+x86_64) | runner default Xcode 16.x lacks macOS 26 SDK; `build.sh` hid the real error (tee + tail-40) | workflow: select newest Xcode (`sort -V`), inline `PYTHON_BIN`, dump+upload `xcodebuild.log` |
| 33781349766 | post-compile staging | Xcode 26.3 `actool` exits 0 but emits no `AppIcon.icns` for the Tahoe `.icon`; unconditional `cp` aborted the run | `build.sh`: guard icon `cp` + Info.plist patch on the artifacts existing; else warn + ship legacy icon (fork commit 74ff125a) |

### Remaining risk (what could still fail)

- Smoke test (`omlx-cli --help`, `omlx-cluster-python -c 'import mlx.core'`)
  could fail if embedded layer paths or ad-hoc signing are off for the
  arm64 runner's macOS version — grab the step log, check `PYTHONPATH`
  assumptions in the wrappers.
- `ditto`/artifact steps are low risk.

## Fork-local edits so far (keep diffs small for a future upstream PR)

1. `omlx/scheduler.py` — publish admin snapshot at `add_request` enqueue +
   make publish race-safe (fixes dashboard stuck "idle" ~10s; merged).
   Test added in `tests/test_scheduler.py`.
2. `apps/omlx-mac/Scripts/build.sh` — guard the actool icon copy (above).
3. `.github/workflows/build-app.yml` — new fork-only workflow.
4. `.github/workflows/ci.yml` — added `workflow_dispatch` trigger.

## Notes

- macOS runner minutes bill ~10x on free plans — dispatch deliberately,
  not on every push (that's why the app workflow is dispatch-only).
- App is ad-hoc signed. On another Mac: right-click → Open once, or
  `xattr -dr com.apple.quarantine oMLX.app`.
- Do NOT push to `origin` (upstream). Only `tsang`.
- `watch-actions.sh` exit codes: 0 all pass, 1 some fail, 2 timeout/error
  (`TIMEOUT_S`, default 3600s).
