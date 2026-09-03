# Remote oMLX.app builds on GitHub (tsang/omlx)

## What we're doing

Upstream `jundot/omlx` does **not** build the macOS app in CI. Per
`packaging/README.md:69-72` the release DMGs come from an off-tree
maintainer pipeline; the repo's GitHub Actions only run tests (`ci.yml`),
wheels (`build-wheels.yml`), and the Homebrew formula bump
(`update-formula.yml`).

So the fork `tsang/omlx` carries its own workflow,
`.github/workflows/build-app.yml`, that builds `oMLX.app` on a hosted
`macos-15` runner and uploads it as an artifact.

## Pipeline (what the workflow does)

1. checkout + select newest Xcode on the runner
2. `actions/setup-python` 3.11, `pip install venvstacks`
3. `apps/omlx-mac/Scripts/build.sh release [--with-custom-kernel]`
   - resolves SPM deps, `xcodebuild` builds the Swift app (Release builds
     **both** arm64 + x86_64; Release config has no `ONLY_ACTIVE_ARCH`)
   - auto-rebuilds the venvstacks Python export when stale
     (`packaging/build.py --venvstacks-only`, ~6 min: `cpython-3.11` +
     `framework-mlx-base` ~1 GB)
   - stages `apps/omlx-mac/build/Stage/oMLX.app`, embeds Python layers +
     the `omlx` package from the worktree, writes `omlx-cli` /
     `omlx-cluster-python` wrappers, compiles the Tahoe `AppIcon.icon`,
     ad-hoc signs everything
4. smoke test through the bundle's own wrappers
   (`omlx-cli --help`, `omlx-cluster-python -c 'import mlx.core'`)
5. `ditto -c -k --keepParent` zip + sha256 → artifact `oMLX-app-<sha>`
6. on failure: dumps `apps/omlx-mac/build/xcodebuild.log` error lines and
   uploads the log as artifact `xcodebuild-log-<sha>`

## How to trigger a build

```bash
cd ~/Sync/devel/omlx
git push tsang main
gh workflow run "Build macOS app" --repo tsang/omlx --ref main
```

Inputs: `configuration` (release|debug), `custom_kernels` (bool; needs full
Xcode + nanobind pins, makes the GLM-5.2/MiniMax M3/Qwen3.5 fast paths
native instead of slow fallbacks).

## How to monitor

```bash
./watch-actions.sh tsang/omlx 30   # prints status transitions, dumps failed logs
```

Manual one-offs:

```bash
gh run list --repo tsang/omlx --limit 5
gh run view <run-id> --repo tsang/omlx --log-failed
gh run download <run-id> --repo tsang/omlx --name xcodebuild-log-<sha>
```

Timing: ~2 min to a Swift/early failure, ~15-30 min for a full build.

## Failure history + root causes

| run | outcome | root cause |
|-----|---------|-----------|
| `33774786542` | Swift compile fail (arm64 + x86_64) | runner default Xcode 16.x lacks the macOS 26 SDK APIs the app uses; diagnostics were invisible because `build.sh` tees xcodebuild output to `build/xcodebuild.log` and only echoes `tail -40` (summary noise) |
| `33781349766` | Swift + Python + embed OK; `cp: …/AppIcon.icns: No such file or directory` | newest runner Xcode is 26.3; its `actool` exits 0 but emits no `.icns` for `AppIcon.icon` (icon-composer output behavior the workaround relies on is from 26.5, the maintainer's version). `build.sh` cp'd the "result" unguarded |

## Fixes applied to the fork

1. workflow: `sort -V` newest-Xcode selection (26.3 beat default 16.x —
   unblocked Swift), inline `PYTHON_BIN`, failure log dump + artifact upload
2. `build.sh` (fork-local): guard the icon step — check
   `$ICON_TMP/AppIcon.icns` + `Assets.car` exist before copying, echo
   `actool` log tail on absence, keep legacy icon instead of aborting.
   Info.plist icon keys are only patched when the icon actually landed.

## The fix-and-cycle loop

1. wait for run to finish (`watch-actions.sh` or `gh run watch <id>`)
2. pull diagnostics: `gh run view <id> --repo tsang/omlx --log-failed`;
   deeper: download `xcodebuild-log-<sha>` artifact (or `actool-icon.log`)
3. fix in `~/Sync/devel/omlx` (workflow in `.github/workflows/`,
   packaging tweaks in `apps/omlx-mac/Scripts/build.sh`)
4. `git push tsang main`
5. `gh workflow run "Build macOS app" --repo tsang/omlx --ref main`
6. repeat until green, then download the `oMLX-app-<sha>` artifact

## Notes

- macOS runner minutes bill 10x on free plans; dispatch deliberately.
- App is ad-hoc signed: on another Mac, right-click → Open once, or
  `xattr -dr com.apple.quarantine oMLX.app`.
- Upstream `build.sh` stays untouched except where the fork needs a
  robustness tweak (keep such diffs small and marked, they must not
  conflict with an eventual upstream PR).
- Nothing else blocks on this machine; all iteration happens remotely.
