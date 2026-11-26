"""End-to-end test: needs Docker + act + `pip install pexpect`. Run from the repo root: python3 tests/e2e_demo.py"""
import pexpect, sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.join(ROOT, "examples", "demo-repo"))
env = dict(os.environ, PYTHONPATH=ROOT, NO_COLOR="1")
c = pexpect.spawn("python3 -m ghdebug run", env=env, encoding="utf-8", timeout=180)
c.logfile_read = sys.stdout
c.expect(r"\(ghdebug step 4\)")
c.sendline('echo "PWD=$(pwd) BUILD_ID=$BUILD_ID EXPECTED=$EXPECTED_GREETING APP_ENV=$APP_ENV"; ls build')
c.expect(r"\(ghdebug step 4\)")
c.sendline("showstep")
c.expect(r"\(ghdebug step 4\)")
# fix the bug inside the container, then rerun the step
c.sendline("sed -i 's/Hello world/hello, world/' greet.sh && rerun")
c.expect(r"exit code: 0")
c.sendline("exit")
c.expect("resume how")
c.sendline("r")
c.expect(pexpect.EOF)
c.close()
print("\nEXIT", c.exitstatus)
os.system("git checkout -q greet.sh 2>/dev/null")  # demo bug lives on the host copy, untouched
assert c.exitstatus == 0, "workflow should succeed after the in-container fix + retry"
print("E2E OK")
