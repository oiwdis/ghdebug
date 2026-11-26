# ghdebug

**Debug GitHub Actions on your machine. When a step fails, the job pauses and you get a shell inside the job container, right at the failure. Fix it, retry the step, keep going.**

No more `git commit -m "fix ci" && git push`, wait 4 minutes, repeat.

ghdebug is a thin wrapper around [nektos/act](https://github.com/nektos/act). act runs your workflow in Docker; ghdebug adds the debugging loop:

- **Pause on failure.** The failing `run:` step stops in place. The container keeps running and nothing gets torn down.
- **Shell at the failure.** You land in the job container with the **same working directory and environment** the step had. That includes `env:` blocks, matrix values, and anything earlier steps wrote to `$GITHUB_ENV`.
- **Retry / continue / fail.** Fix the problem, type `exit`, then press `r` to rerun the step, `c` to mark it passed, or `f` to let the job fail.
- **Rerun from step N.** `ghdebug run --from-step 4` skips steps 1–3. It reuses the container from the last run, so build outputs are still there, and it restores the env vars those steps exported.

```
[CI/test]   | FAIL: expected 'hello, world', got 'Hello world'

[ghdebug] step 4 (Test) in job 'test' failed with exit code 1. Job is PAUSED.
[ghdebug] opening a shell in container 08dda3f10cce ...

  ghdebug: you're inside the job container, paused at failed step 4.
  cwd + env match that step. Helpers:  showstep  (print the step's script)   rerun  (run it again)

(ghdebug step 4) /src/demo-repo # echo $BUILD_ID $EXPECTED_GREETING
build-1795736360 hello, world
(ghdebug step 4) /src/demo-repo # sed -i 's/Hello world/hello, world/' greet.sh && rerun
PASS
[exit code: 0]
(ghdebug step 4) /src/demo-repo # exit
[ghdebug] resume how?  [r]etry step  [c]ontinue (mark passed)  [f]ail  [s]hell again > r
[CI/test]   | [ghdebug] retrying step 4
[CI/test]   | PASS
[CI/test]   ✅  Success - Main Test
[CI/test]   ✅  Success - Main Deploy (dry run)
[ghdebug] workflow succeeded
```

## Install

You need **Docker** (running), **act** ≥ 0.2.60, and **Python 3.9+**.

```bash
# act: https://nektosact.com/installation/  (e.g. `brew install act`)
pipx install git+https://github.com/<you>/ghdebug   # or: pip install .  from a clone
ghdebug --version
```

ghdebug maps `ubuntu-latest` to `catthehacker/ubuntu:act-latest`, about 1.6 GB on first pull. It only does this if you haven't set platforms in `~/.actrc` or passed `-P`.

## 30-second demo

```bash
cd examples/demo-repo
ghdebug steps          # list the numbered steps
ghdebug run            # step 4 "Test" fails -> you're dropped into a shell
#   inside the shell:
#     showstep                                    # what the step ran
#     echo $BUILD_ID                              # set by step 2 via $GITHUB_ENV
#     sed -i 's/Hello world/hello, world/' greet.sh && rerun
#     exit   -> press r -> the job finishes green
```

Rerun from a step after fixing the code on your host:

```bash
sed -i 's/Hello world/hello, world/' greet.sh
ghdebug run --from-step 4     # skips Install + Build, reuses their outputs, restores BUILD_ID
git checkout greet.sh         # put the demo bug back
```

## Commands

| command | what it does |
|---|---|
| `ghdebug run [event]` | Run the workflow with act and pause on a failing `run:` step. `event` defaults to `push`. |
| `ghdebug run --from-step N -j JOB` | Skip steps before N. Reuses the kept container and restores `$GITHUB_ENV`/`$GITHUB_PATH` from the last run. |
| `ghdebug run --no-pause` | Plain act behaviour. The container is still kept for `ghdebug shell`. |
| `ghdebug run --no-shell` | On failure, ask retry/continue/fail without opening a shell first. |
| `ghdebug run -- <act args>` | Anything after `--` goes to act, e.g. `-- --secret-file .secrets --env FOO=1`. |
| `ghdebug steps` | List jobs and step numbers. |
| `ghdebug shell [-j JOB] [--step N]` | Shell into a kept job container with env/cwd as of step N. Works after a run has finished, including when a `uses:` step failed. |
| `ghdebug clean` | Remove the containers act/ghdebug kept. |

`-W path/to/workflow.yml` picks a workflow when the repo has more than one. `-j job` picks a job.
For scripts and CI, set `GHDEBUG_ON_RESUME=retry|continue|fail` to answer the resume prompt automatically.

## How it works

1. ghdebug reads your workflow and writes a copy to `.ghdebug/<file>.yml`. In the copy, each `run:` step is wrapped in a small POSIX-sh wrapper:
   - Your script goes, unchanged, into a quoted heredoc. act still expands `${{ }}` expressions as usual.
   - Before the script runs, the wrapper saves `export -p`, `env -0` and `pwd` to `/tmp/ghdebug/step-N.*` inside the container.
   - It runs your script with the same shell GitHub would use: `bash -e`, or `bash --noprofile --norc -eo pipefail` for `shell: bash`, or `sh -e`, `python`, or a custom `{0}` shell.
   - If the script fails, the wrapper prints a marker and waits for a resume file.
2. act runs the copy with `--reuse`, so job containers survive failures.
3. ghdebug watches act's output for the marker, finds the paused container, and runs `docker exec -it ... bash --rcfile` with the saved env and cwd loaded. When you exit, it writes your choice (retry/continue/fail) to the resume file.
4. `--from-step N` replaces the `if:` of earlier steps with `${{ false }}`. It doesn't skip `actions/checkout`, because that's the step that copies your edited code into the container. It reuses the kept container, so files built by earlier steps are still there. It compares the env snapshots of the first step and step N, then re-exports what the skipped steps added through `$GITHUB_ENV`/`$GITHUB_PATH`.

## Limitations (prototype)

- **Only `run:` steps pause in place.** A failing `uses:` action (JS/Docker/composite) fails as normal. The container is kept, so `ghdebug shell -j JOB` still gets you in.
- `--from-step` can't restore **step outputs** (`steps.<id>.outputs.*`) from skipped steps, or files written outside the container (e.g. caches, artifacts). Anything that depends on those needs the step to run again.
- `pwsh`/`powershell`/`cmd` steps aren't wrapped. They run normally.
- Steps with `continue-on-error: true` don't pause.
- When jobs run in parallel, their logs are held back while you're in a shell and printed afterwards. Each paused job is handled one at a time.
- act itself isn't GitHub. Some actions, services and runner images behave differently. See [act's docs](https://nektosact.com/).
- Linux and macOS (Docker Desktop/Colima/OrbStack). Windows has only been tried through WSL2.

## Tests

```bash
pip install pexpect
python3 tests/e2e_demo.py   # drives a real pty: run -> pause -> fix in shell -> retry -> green
```

## License

MIT
