"""Source selection must preserve both accepted merge and original proof tuple."""

import subprocess

import pytest

from tests.local_speed_qualification import PIN_READER, PIN_REFERENCE, WORKFLOW, validate_sources


def run(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=Meter proof",
                           "-c", "user.email=meter-proof@example.invalid", *args],
                          capture_output=True, text=True, check=check).stdout.strip()


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(root, message):
    run(root, "add", ".")
    run(root, "commit", "-qm", message)
    return run(root, "rev-parse", "HEAD")


def graph(root, change=None):
    run(root, "init", "-q")
    write(root, WORKFLOW, "name: meter\n# Runtime checkout\n")
    write(root, "astrid/product.py", "original product\n")
    write(root, "runtime.lock.json", '{"source_commit": "original-runtime"}\n')
    write(root, "tests/fixtures/local-speed-baselines/linux.json", '{"immutable": true}\n')
    write(root, "tests/_local_speed_process.py", "original observer\n")
    original = commit(root, "original tuple")

    accepted_workflow = "name: meter\n" + PIN_READER + PIN_REFERENCE
    write(root, WORKFLOW, accepted_workflow)
    write(root, "astrid/product.py", "accepted product\n")
    ancestor = commit(root, "accepted LS-7")
    for path in ("tests/helpers/ls5_comfy_peer.py", "tests/integrations/test_ls5_meter.py"):
        write(root, path, "accepted new harness\n")
    write(root, "runtime.lock.json", '{"source_commit": "accepted-runtime"}\n')
    plan = commit(root, "accepted LS-4")

    run(root, "checkout", "--detach", original)
    qualified_workflow = "name: meter\npython -m tests.local_speed_qualification\n" + PIN_READER + PIN_REFERENCE
    write(root, WORKFLOW, qualified_workflow)
    write(root, "tests/_local_speed_process.py", "qualified observer\n")
    if change in {"product", "dependency", "reference"}:
        path = {"product": "astrid/product.py", "dependency": "runtime.lock.json",
                "reference": "tests/fixtures/local-speed-baselines/linux.json"}[change]
        write(root, path, "unapproved original-tuple change\n")
    qualified = commit(root, "meter correction")
    run(root, "merge", "--no-commit", "--no-ff", plan, check=False)
    write(root, WORKFLOW, qualified_workflow)
    # Product/dependency conflict injections still get an integrated candidate;
    # the qualified-source guard, rather than an incidental conflict, rejects it.
    if change in {"product", "dependency"}:
        path = "astrid/product.py" if change == "product" else "runtime.lock.json"
        write(root, path, run(root, "show", plan + ":" + path) + "\n")
    if change == "lost_accepted":
        write(root, "astrid/product.py", "original product\n")
    elif change == "harness":
        write(root, "tests/_local_speed_process.py", "different measurement observer\n")
    elif change == "workflow":
        write(root, WORKFLOW, accepted_workflow)
    controller = commit(root, "integrated candidate")
    return {"root": root, "controller": controller, "original_head": original,
            "plan_base": plan, "required_ancestor": ancestor}, qualified


def test_actual_native_merge_preserves_the_original_tuple_and_accepted_changes(tmp_path):
    args, qualified = graph(tmp_path)
    receipt = validate_sources(**args)
    assert receipt["qualification"] == qualified
    assert receipt["plan_base"] == args["plan_base"]
    assert receipt["new_plan_tuple_qualified"] is False
    assert receipt["native_merge_conflicts"] == [WORKFLOW]
    assert "tests/_local_speed_process.py" in receipt["measurement_harness_sha256"]
    assert run(tmp_path, "rev-parse", "HEAD") == args["controller"]
    assert run(tmp_path, "show", "HEAD:astrid/product.py") == "accepted product"


@pytest.mark.parametrize("change,reason", [
    ("product", "protected"), ("dependency", "protected"), ("reference", "protected"),
    ("lost_accepted", "native merge"), ("harness", "native merge"),
    ("workflow", "workflow differs"),
])
def test_source_handoff_rejects_changed_protected_or_accepted_bytes(tmp_path, change, reason):
    args, _ = graph(tmp_path, change)
    with pytest.raises(ValueError, match=reason):
        validate_sources(**args)


def test_source_handoff_rejects_a_dirty_controller(tmp_path):
    args, _ = graph(tmp_path)
    write(tmp_path, "astrid/product.py", "uncommitted\n")
    with pytest.raises(ValueError, match="clean exact"):
        validate_sources(**args)


def test_source_handoff_rejects_the_wrong_plan_parent(tmp_path):
    args, _ = graph(tmp_path)
    args["plan_base"] = args["required_ancestor"]
    with pytest.raises(ValueError, match="ordered.*parents"):
        validate_sources(**args)
