from pathlib import Path
import re
import subprocess
import sys
import yaml

path = Path(".github/workflows/lanes.yml")
workflow = yaml.safe_load(path.read_text())
source = yaml.safe_load(subprocess.check_output(["git", "show", "ef8df6eb25c5de1691c357c2a6d0e3b87f5074c2:.github/workflows/lanes.yml"], text=True))
assert set(workflow["jobs"]) == set(source["jobs"])
assert {k: v for k, v in workflow["jobs"].items() if k != "installed"} == {k: v for k, v in source["jobs"].items() if k != "installed"}
print("YAML parsed; non-installed jobs identical to #165")
count = 0
python_count = 0
for step in workflow["jobs"]["installed"]["steps"]:
    body = step.get("run")
    if body is None:
        continue
    subprocess.run(["bash", "-n"], input=body, text=True, check=True)
    count += 1
    for match in re.finditer(r"<<'PY'\n(.*?)\nPY(?:\n|$)", body, re.S):
        compile(match.group(1), step.get("name", "embedded Python"), "exec")
        python_count += 1
print(f"bash -n passed for {count} installed-job shell bodies; {python_count} embedded Python blocks compiled")
assert count == 10 and python_count == 2
