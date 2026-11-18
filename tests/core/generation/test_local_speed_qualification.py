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
    target.write_bytes(text.encode("utf-8"))


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


def test_followup_retains_published_history_without_changing_either_tuple(tmp_path):
    args, qualified = graph(tmp_path)
    previous = args["controller"]
    run(tmp_path, "checkout", "--detach", qualified)
    write(tmp_path, "tests/_local_speed_process.py", "qualified follow-up observer\n")
    source = commit(tmp_path, "follow-up on original tuple")
    run(tmp_path, "checkout", "--detach", previous)
    run(tmp_path, "merge", "--no-commit", "--no-ff", source)
    controller = commit(tmp_path, "retain published controller")
    args.update(controller=controller, previous_controller=previous)
    receipt = validate_sources(**args)
    assert receipt["previous_controller_preserved"] == previous
    assert receipt["qualification"] == source
    assert run(tmp_path, "merge-base", previous, controller) == previous
    args["previous_controller"] = qualified
    with pytest.raises(ValueError, match="exact published controller"):
        validate_sources(**args)


def test_followup_then_new_plan_merge_keeps_both_native_merges_and_original_tuple(tmp_path):
    args, qualified = graph(tmp_path)
    previous, old_plan = args["controller"], args["plan_base"]
    run(tmp_path, "checkout", "--detach", qualified)
    write(tmp_path, "tests/_local_speed_process.py", "SDK invariant observer\n")
    source = commit(tmp_path, "original-tuple SDK fix")
    run(tmp_path, "checkout", "--detach", previous)
    run(tmp_path, "merge", "--no-commit", "--no-ff", source)
    followup = commit(tmp_path, "retain controller and SDK fix")
    run(tmp_path, "checkout", "--detach", old_plan)
    write(tmp_path, "astrid/product.py", "new accepted plan product\n")
    plan = commit(tmp_path, "accepted LS-8")
    run(tmp_path, "checkout", "--detach", followup)
    run(tmp_path, "merge", "--no-commit", "--no-ff", plan)
    controller = commit(tmp_path, "merge newly accepted plan")
    args.update(controller=controller, plan_base=plan, previous_plan_base=old_plan,
                previous_controller=previous)
    receipt = validate_sources(**args)
    assert receipt["qualification"] == source
    assert receipt["previous_controller_preserved"] == previous
    assert receipt["retained_followup_merge"]["controller"] == followup
    assert receipt["retained_followup_merge"]["native_merge_tree"] == run(tmp_path, "rev-parse", followup + "^{tree}")
    assert receipt["native_merge_tree"] == run(tmp_path, "rev-parse", controller + "^{tree}")
    assert receipt["native_merge_conflicts"] == []
    assert run(tmp_path, "show", controller + ":astrid/product.py") == "new accepted plan product"
    assert run(tmp_path, "show", source + ":astrid/product.py") == "original product"
    assert run(tmp_path, "show", source + ":runtime.lock.json") == '{"source_commit": "original-runtime"}'
    assert receipt["new_plan_tuple_qualified"] is False
    run(tmp_path, "checkout", "--detach", source)
    write(tmp_path, "tests/_local_speed_process.py", "final SDK evidence correction\n")
    corrected_source = commit(tmp_path, "correct original-tuple evidence")
    run(tmp_path, "checkout", "--detach", controller)
    run(tmp_path, "merge", "--no-commit", "--no-ff", corrected_source)
    corrected = commit(tmp_path, "preserve both accepted plan merges")
    args.update(controller=corrected, previous_controller=controller)
    receipt = validate_sources(**args)
    assert receipt["qualification"] == corrected_source
    assert receipt["previous_controller_preserved"] == controller
    assert run(tmp_path, "merge-base", controller, corrected) == controller
    assert run(tmp_path, "show", corrected + ":astrid/product.py") == "new accepted plan product"
    assert receipt["new_plan_tuple_qualified"] is False


def test_fixture_preserves_exact_lf_bytes_with_windows_text_defaults(tmp_path, monkeypatch):
    original = type(tmp_path).write_text

    def windows_write_text(path, data, encoding=None, errors=None, newline=None):
        return original(path, data, encoding=encoding, errors=errors,
                        newline="\r\n" if newline is None else newline)

    monkeypatch.setattr(type(tmp_path), "write_text", windows_write_text)
    test_actual_native_merge_preserves_the_original_tuple_and_accepted_changes(tmp_path)


def test_successive_followups_retain_each_published_controller(tmp_path):
    args, source = graph(tmp_path)
    previous = args["controller"]
    for index in range(2):
        git_before = previous
        run(tmp_path, "checkout", "--detach", source)
        write(tmp_path, "tests/_local_speed_process.py", f"follow-up observer {index}\n")
        source = commit(tmp_path, f"original-tuple follow-up {index}")
        run(tmp_path, "checkout", "--detach", previous)
        run(tmp_path, "merge", "--no-commit", "--no-ff", source)
        controller = commit(tmp_path, f"retain published controller {index}")
        args.update(controller=controller, previous_controller=previous)
        receipt = validate_sources(**args)
        assert receipt["previous_controller_preserved"] == git_before
        assert receipt["qualification"] == source
        assert run(tmp_path, "merge-base", git_before, controller) == git_before
        previous = controller
