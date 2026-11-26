"""ghdebug - pause-on-failure debugging for GitHub Actions, on top of nektos/act."""
import argparse
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading

import yaml

from ghdebug import __version__

DEFAULT_PLATFORMS = [
    "ubuntu-latest=catthehacker/ubuntu:act-latest",
    "ubuntu-24.04=catthehacker/ubuntu:act-24.04",
    "ubuntu-22.04=catthehacker/ubuntu:act-22.04",
]
STATE = "/tmp/ghdebug"  # inside the job container
MARKER = re.compile(r"GHDEBUG_PAUSED step=(\d+) rc=(\d+) nonce=(\S+) job=(\S+)")

C_Y, C_R, C_G, C_B, C_0 = "\033[1;33m", "\033[1;31m", "\033[1;32m", "\033[1m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C_Y = C_R = C_G = C_B = C_0 = ""


def say(msg, color=C_Y):
    print(f"{color}[ghdebug]{C_0} {msg}", flush=True)


# --------------------------------------------------------------------------- YAML
class _Loader(yaml.SafeLoader):
    """SafeLoader that does not turn `on:` / `yes` / `no` into booleans (YAML 1.1 gotcha)."""


_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class _Dumper(yaml.SafeDumper):
    pass


def _str_rep(dumper, s):
    if "\n" in s:
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", s)


_Dumper.add_representer(str, _str_rep)


def load_workflow(path):
    with open(path) as f:
        return yaml.load(f, Loader=_Loader)


# ------------------------------------------------------------------- step helpers
def step_label(step):
    if step.get("name"):
        return str(step["name"])
    if step.get("uses"):
        return f"uses: {step['uses']}"
    if step.get("run"):
        return "run: " + str(step["run"]).strip().splitlines()[0][:60]
    return "(step)"


def is_checkout(step):
    return str(step.get("uses", "")).startswith("actions/checkout")


def resolve_shell(step, job, wf):
    for scope in (step, (job.get("defaults") or {}).get("run") or {}, (wf.get("defaults") or {}).get("run") or {}):
        if scope.get("shell"):
            return str(scope["shell"])
    return None


def runner_for(shell):
    """Return a sh command template running the user's script ("$F"), or None if unsupported."""
    if shell is None:
        return 'if command -v bash >/dev/null 2>&1; then bash -e "$F"; else sh -e "$F"; fi'
    if shell == "bash":
        return 'bash --noprofile --norc -eo pipefail "$F"'
    if shell == "sh":
        return 'sh -e "$F"'
    if shell == "python":
        return 'python "$F"'
    if shell in ("pwsh", "powershell", "cmd"):
        return None
    if "{0}" in shell:
        return shell.replace("{0}", '"$F"')
    return None


def wrap_run(step_no, job_id, step, job, wf, pause, restore_lines=()):
    """Turn a `run:` step into a POSIX-sh wrapper that snapshots env and can pause on failure."""
    runner = runner_for(resolve_shell(step, job, wf))
    if runner is None:
        return None
    delim = "__GHD_SCRIPT_" + secrets.token_hex(6) + "__"
    script = str(step["run"])
    if not script.endswith("\n"):
        script += "\n"
    pause_block = (
        f"""  __ghd_nonce="$(date +%s)-$$"
  echo "$__ghd_rc" > "$D/paused-$__ghd_nonce"
  echo "GHDEBUG_PAUSED step={step_no} rc=$__ghd_rc nonce=$__ghd_nonce job={job_id}"
  while [ ! -f "$D/resume-$__ghd_nonce" ]; do sleep 0.3; done
  __ghd_act="$(cat "$D/resume-$__ghd_nonce")"
  rm -f "$D/resume-$__ghd_nonce" "$D/paused-$__ghd_nonce"
  case "$__ghd_act" in
    retry) echo "[ghdebug] retrying step {step_no}"; continue ;;
    continue) echo "[ghdebug] step {step_no} marked as passed by user"; exit 0 ;;
    *) exit "$__ghd_rc" ;;
  esac
"""
        if pause
        else '  exit "$__ghd_rc"\n'
    )
    restore = "".join(l + "\n" for l in restore_lines)
    return f"""# --- ghdebug wrapper (step {step_no}) ---
D={STATE}; mkdir -p "$D"; F="$D/step-{step_no}.script"
cat > "$F" <<'{delim}'
{script}{delim}
{restore}echo {shlex.quote(job_id)} > "$D/job"; echo {step_no} > "$D/last-step"
export -p > "$D/step-{step_no}.sh"
(env -0 > "$D/step-{step_no}.env0") 2>/dev/null || true
pwd > "$D/step-{step_no}.pwd"
printf '%s\\n' {shlex.quote(runner)} > "$D/step-{step_no}.runner"
while :; do
  {runner}
  __ghd_rc=$?
  [ "$__ghd_rc" -eq 0 ] && exit 0
{pause_block}done
"""


def transform(wf, only_job=None, from_step=None, pause=True, restore_lines=(), skip_checkout=False):
    """Return (new_workflow, notes)."""
    notes = []
    wf = yaml.load(yaml.dump(wf, Dumper=_Dumper), Loader=_Loader)  # deep copy
    for job_id, job in (wf.get("jobs") or {}).items():
        if not isinstance(job, dict) or "steps" not in job:
            continue
        for i, step in enumerate(job["steps"], start=1):
            if from_step and (only_job in (None, job_id)) and i < from_step:
                if is_checkout(step) and not skip_checkout:
                    continue  # keep: this is what copies your (possibly fixed) code into the container
                step["if"] = "${{ false }}"
                continue
            if "run" in step:
                restore = restore_lines if (from_step and i == from_step and only_job in (None, job_id)) else ()
                p = pause and not step.get("continue-on-error")
                new = wrap_run(i, job_id, step, job, wf, p, restore)
                if new is None:
                    notes.append(f"{job_id} step {i}: shell '{resolve_shell(step, job, wf)}' not supported, left unwrapped")
                    continue
                if not step.get("name"):  # otherwise act would display the wrapper as the step name
                    lines = str(step["run"]).strip().splitlines()
                    step["name"] = lines[0] + (" ..." if len(lines) > 1 else "")
                step["run"] = new
                step["shell"] = "sh {0}"
    return wf, notes


# --------------------------------------------------------------------- discovery
def find_workflow(repo, wpath):
    if wpath:
        return os.path.abspath(wpath)
    d = os.path.join(repo, ".github", "workflows")
    files = sorted(f for f in os.listdir(d) if f.endswith((".yml", ".yaml"))) if os.path.isdir(d) else []
    if len(files) == 1:
        return os.path.join(d, files[0])
    if not files:
        sys.exit("ghdebug: no workflows found in .github/workflows (use -W)")
    sys.exit("ghdebug: multiple workflows found, pick one with -W:\n  " + "\n  ".join(files))


def docker(*args, capture=True, check=False, **kw):
    return subprocess.run(["docker", *args], capture_output=capture, text=True, check=check, **kw)


def act_containers():
    out = docker("ps", "--filter", "name=^act-", "--format", "{{.ID}} {{.Names}}").stdout
    return [line.split(" ", 1) for line in out.splitlines() if line.strip()]


def container_job(cid):
    r = docker("exec", cid, "cat", f"{STATE}/job")
    return r.stdout.strip() if r.returncode == 0 else None


def find_job_container(job=None):
    matches = []
    for cid, name in act_containers():
        j = container_job(cid)
        if j and (job is None or j == job):
            matches.append((cid, name, j))
    return matches


def find_paused_container(nonce):
    for cid, _ in act_containers():
        if docker("exec", cid, "test", "-f", f"{STATE}/paused-{nonce}").returncode == 0:
            return cid
    return None


def read_env0(cid, step):
    r = subprocess.run(["docker", "exec", cid, "cat", f"{STATE}/step-{step}.env0"], capture_output=True)
    if r.returncode != 0:
        return None
    env = {}
    for item in r.stdout.decode(errors="replace").split("\0"):
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
    return env


def snapshot_steps(cid):
    r = docker("exec", cid, "sh", "-c", f"ls {STATE} 2>/dev/null")
    return sorted({int(m.group(1)) for m in re.finditer(r"step-(\d+)\.sh", r.stdout)})


# ------------------------------------------------------------------ debug shell
def open_shell(cid, step, failed=False):
    rc = f"""[ -f ~/.bashrc ] && . ~/.bashrc
bind 'set enable-bracketed-paste off' 2>/dev/null
printf '\033[?2004l' 2>/dev/null
. {STATE}/step-{step}.sh 2>/dev/null
cd "$(cat {STATE}/step-{step}.pwd 2>/dev/null || echo .)"
rerun() {{ F={STATE}/step-{step}.script; eval "$(cat {STATE}/step-{step}.runner)"; echo "[exit code: $?]"; }}
showstep() {{ cat {STATE}/step-{step}.script; }}
PS1='\\[\\e[1;33m\\](ghdebug step {step})\\[\\e[0m\\] \\w \\$ '
echo
echo "  ghdebug: you're inside the job container{', paused at failed step ' + str(step) if failed else ', env as of step ' + str(step)}."
echo "  cwd + env match that step. Helpers:  showstep  (print the step's script)   rerun  (run it again)"
echo "  Type 'exit' when done."
echo
"""
    docker("exec", "-i", cid, "sh", "-c", f"cat > {STATE}/rc", input=rc)
    shell = "bash" if docker("exec", cid, "sh", "-c", "command -v bash").returncode == 0 else "sh"
    cmd = ["docker", "exec", "-it", cid, "bash", "--rcfile", f"{STATE}/rc", "-i"] if shell == "bash" else \
          ["docker", "exec", "-it", "-e", f"ENV={STATE}/rc", cid, "sh", "-i"]
    if not sys.stdin.isatty():
        cmd.remove("-it")
        cmd.insert(2, "-i")
    return subprocess.call(cmd)


def ask_action(default="fail"):
    env_choice = os.environ.get("GHDEBUG_ON_RESUME")
    if env_choice:
        return env_choice
    if not sys.stdin.isatty():
        return default
    while True:
        try:
            a = input(f"{C_Y}[ghdebug]{C_0} resume how?  [r]etry step  [c]ontinue (mark passed)  [f]ail  [s]hell again > ").strip().lower()
        except EOFError:
            return default
        a = a[:1] or "f"
        if a in "rcfs":
            return {"r": "retry", "c": "continue", "f": "fail", "s": "shell"}[a]


# -------------------------------------------------------------------- commands
def act_base_args(args):
    a = ["act", args.event, "--reuse"]
    if not args.pull:
        a.append("--pull=false")
    have_platform = any(x in ("-P", "--platform") or x.startswith("--platform=") for x in args.act_args)
    have_actrc = os.path.exists(os.path.expanduser("~/.actrc")) or os.path.exists(".actrc")
    if not have_platform and not have_actrc:
        for p in DEFAULT_PLATFORMS:
            a += ["-P", p]
    if args.job:
        a += ["-j", args.job]
    return a


def compute_restore(cid, from_step, steps, job_id):
    """Recreate env/PATH that skipped steps exported via $GITHUB_ENV / $GITHUB_PATH."""
    snaps = snapshot_steps(cid)
    if from_step not in snaps:
        say(f"no env snapshot for step {from_step} in the reused container; skipped steps' $GITHUB_ENV won't be restored", C_R)
        return []
    base_step = snaps[0]
    base, target = read_env0(cid, base_step), read_env0(cid, from_step)
    if base is None or target is None:
        return []
    own = set((steps[from_step - 1].get("env") or {}).keys()) | set((steps[base_step - 1].get("env") or {}).keys())
    lines, restored = [], []
    for k, v in target.items():
        if k in own or k in ("PATH", "PWD", "OLDPWD", "SHLVL", "_") or k.startswith(("GITHUB_", "RUNNER_", "ACT", "INPUT_", "STATE_")):
            continue
        if base.get(k) != v:
            lines.append(f"export {k}={shlex.quote(v)}")
            lines.append(f"{{ echo '{k}<<__GHD_EOF__'; printf '%s\\n' {shlex.quote(v)}; echo '__GHD_EOF__'; }} >> \"$GITHUB_ENV\"")
            restored.append(k)
    bp, tp = base.get("PATH", "").split(":"), target.get("PATH", "").split(":")
    added = [p for p in tp if p and p not in bp]
    for p in reversed(added):
        lines.append(f"echo {shlex.quote(p)} >> \"$GITHUB_PATH\"")
    if added:
        lines.append(f"export PATH={shlex.quote(':'.join(added))}:\"$PATH\"")
    if restored or added:
        say(f"restoring env from previous run: {', '.join(restored + (['PATH+=' + ':'.join(added)] if added else []))}")
    return lines


def cmd_steps(args):
    wf = load_workflow(find_workflow(os.getcwd(), args.workflow))
    for job_id, job in (wf.get("jobs") or {}).items():
        if args.job and job_id != args.job:
            continue
        print(f"{C_B}{job_id}{C_0}")
        for i, s in enumerate((job or {}).get("steps") or [], start=1):
            print(f"  {i:>2}. {step_label(s)}")
    return 0


def cmd_run(args):
    if not shutil.which("act"):
        sys.exit("ghdebug: `act` not found. Install it: https://nektosact.com/installation/")
    if docker("info").returncode != 0:
        sys.exit("ghdebug: can't talk to Docker. Is the daemon running?")
    repo = os.getcwd()
    wpath = find_workflow(repo, args.workflow)
    wf = load_workflow(wpath)
    jobs = wf.get("jobs") or {}
    if args.from_step:
        if not args.job:
            if len(jobs) != 1:
                sys.exit("ghdebug: --from-step needs -j <job> when the workflow has several jobs")
            args.job = next(iter(jobs))
        steps = jobs[args.job].get("steps") or []
        if not 1 <= args.from_step <= len(steps):
            sys.exit(f"ghdebug: job '{args.job}' has {len(steps)} steps")

    restore = []
    if args.from_step:
        found = find_job_container(args.job)
        if found:
            cid = found[0][0]
            say(f"reusing container {found[0][1][:40]}... (state from the previous run is kept)")
            restore = compute_restore(cid, args.from_step, steps, args.job)
        else:
            say("no kept container from a previous run; skipped steps' side effects won't exist", C_R)
        say(f"skipping steps 1-{args.from_step - 1} (checkout still runs so your edits are copied in)")

    new, notes = transform(wf, args.job, args.from_step, pause=not args.no_pause,
                           restore_lines=restore, skip_checkout=args.skip_checkout)
    for n in notes:
        say(n, C_R)
    if not new.get("name"):
        new["name"] = os.path.basename(wpath)
    gdir = os.path.join(repo, ".ghdebug")
    os.makedirs(gdir, exist_ok=True)
    with open(os.path.join(gdir, ".gitignore"), "w") as f:
        f.write("*\n")
    gen = os.path.join(gdir, os.path.basename(wpath))
    with open(gen, "w") as f:
        f.write(f"# generated by ghdebug from {os.path.relpath(wpath, repo)} - do not edit\n")
        yaml.dump(new, f, Dumper=_Dumper, sort_keys=False, width=1000)

    cmd = act_base_args(args) + ["-W", gen] + args.act_args
    say("running: " + " ".join(shlex.quote(c) for c in cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, bufsize=1)
    q = queue.Queue()

    def pump():
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    threading.Thread(target=pump, daemon=True).start()
    try:
        return _drive(proc, q, args, jobs)
    except KeyboardInterrupt:
        say("interrupted: stopping act (job containers are kept; `ghdebug clean` removes them)", C_R)
        for cid, _ in act_containers():  # release any step still waiting for a resume signal
            docker("exec", cid, "sh", "-c", f"for f in {STATE}/paused-*; do [ -e \"$f\" ] && echo fail > {STATE}/resume-${{f##*/paused-}}; done")
        proc.terminate()
        return 130


def _drive(proc, q, args, jobs):
    paused_any = started = False
    while True:
        line = q.get()
        if line is None:
            break
        if "⭐ Run" in line:
            started = True
        m = MARKER.search(line)
        if not m:
            sys.stdout.write(line)
            sys.stdout.flush()
            continue
        step, rc, nonce, job_id = int(m.group(1)), m.group(2), m.group(3), m.group(4)
        paused_any = True
        label = step_label(jobs[job_id]["steps"][step - 1]) if job_id in jobs else ""
        print()
        say(f"step {step} ({label}) in job '{job_id}' failed with exit code {rc}. Job is PAUSED.", C_R)
        cid = find_paused_container(nonce)
        if not cid:
            say("couldn't locate the paused container; failing the step", C_R)
            continue
        cname = docker("inspect", "-f", "{{.Name}}", cid).stdout.strip().lstrip("/")
        say(f"container: {cname[:20]}…" if len(cname) > 24 else f"container: {cname}")
        action = "shell" if not args.no_shell else ask_action()
        while action == "shell":
            say(f"opening a shell in container {cid[:12]} ...")
            open_shell(cid, step, failed=True)
            action = ask_action()
        docker("exec", cid, "sh", "-c", f"echo {action} > {STATE}/resume-{nonce}")
        say(f"resuming: {action}")
    proc.wait()
    print()
    if proc.returncode == 0:
        say("workflow succeeded", C_G)
    else:
        say(f"act exited with {proc.returncode}.", C_R)
        if not started:
            return proc.returncode
        if args.no_pause:
            say("--no-pause was set.")
        say("failed `uses:` steps can't pause in place, but job containers are kept: inspect one with  ghdebug shell -j <job>")
        say("after fixing, rerun from a step with:  ghdebug run --from-step N" + (f" -j {args.job}" if args.job else ""))
    return proc.returncode


def cmd_shell(args):
    found = find_job_container(args.job)
    if not found:
        sys.exit("ghdebug: no kept job container found. Run `ghdebug run` first.")
    if len(found) > 1 and not args.job:
        sys.exit("ghdebug: several job containers, pick one with -j: " + ", ".join(sorted({f[2] for f in found})))
    cid = found[0][0]
    step = args.step
    if step is None:
        step = int(docker("exec", cid, "cat", f"{STATE}/last-step").stdout.strip() or 1)
    return open_shell(cid, step)


def cmd_clean(args):
    ids = [c[0] for c in act_containers()]
    out = docker("ps", "-a", "--filter", "name=^act-", "-q").stdout.split()
    ids = sorted(set(ids) | set(out))
    if ids:
        docker("rm", "-f", *ids)
    say(f"removed {len(ids)} act container(s)")
    vols = [v for v in docker("volume", "ls", "-q", "--filter", "name=^act-").stdout.split() if v]
    if vols:
        docker("volume", "rm", "-f", *vols)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="ghdebug", description="Debug GitHub Actions locally: pause on failure, shell in, rerun from any step.")
    p.add_argument("--version", action="version", version=f"ghdebug {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("-W", "--workflow", help="workflow file (default: the only one in .github/workflows)")
        sp.add_argument("-j", "--job", help="job id")

    r = sub.add_parser("run", help="run a workflow with act; pause + shell on failure")
    common(r)
    r.add_argument("event", nargs="?", default="push", help="event to simulate (default: push)")
    r.add_argument("--from-step", type=int, metavar="N", help="skip steps before N (reuses the container from the last run)")
    r.add_argument("--no-pause", action="store_true", help="don't pause on failure (plain act behaviour, container still kept)")
    r.add_argument("--no-shell", action="store_true", help="on failure, ask what to do instead of opening a shell first")
    r.add_argument("--skip-checkout", action="store_true", help="with --from-step, also skip actions/checkout")
    r.add_argument("--pull", action="store_true", help="force-pull images (default: use local copies)")
    r.set_defaults(fn=cmd_run)
    r.epilog = "Anything after `--` is passed to act, e.g.  ghdebug run -- --secret-file .secrets"

    s = sub.add_parser("steps", help="list jobs and numbered steps")
    common(s)
    s.set_defaults(fn=cmd_steps)

    sh = sub.add_parser("shell", help="open a shell in the kept job container")
    sh.add_argument("-j", "--job")
    sh.add_argument("--step", type=int, help="load env/cwd as of step N (default: last step that ran)")
    sh.set_defaults(fn=cmd_shell)

    c = sub.add_parser("clean", help="remove containers kept by ghdebug/act")
    c.set_defaults(fn=cmd_clean)

    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    args = p.parse_args(argv)
    args.act_args = extra  # everything after `--` goes straight to act
    return args.fn(args)
