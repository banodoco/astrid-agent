def run(request_oid, bundle_oid, request, input_bundle):
    from astrid.sdk.invocation import invoke_result
    objects = {"request": request_oid, "input_bundle": bundle_oid}
    result = invoke_result("h3_av.prepare", kind="executor",
        inputs={name: {"object_id": oid, "digest": oid, "filename": name + ".bin"}
                for name, oid in objects.items()},
        child_key="prepare", stage="prepare",
        input_refs=[{"name": name, "root_object_id": oid} for name, oid in objects.items()],
        timeout_seconds=30)
    return {"child_id": result.kernel_task_id}
