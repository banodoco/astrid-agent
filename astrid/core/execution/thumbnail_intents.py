"""Host reconciliation of Runtime-owned generation enhancement intents."""
from __future__ import annotations

from collections.abc import Mapping
import logging

CAPABILITY_ID = "generation.thumbnail_backfill"


def bind_thumbnail_output(capability_id, outputs, task):
    """Derive thumbnail provenance only from the already admitted effect."""
    if capability_id != CAPABILITY_ID:
        return
    effect = task.get("expected_effect") or task.get("effect")
    payload = effect.get("payload") if isinstance(effect, Mapping) else None
    spec = task.get("spec") or {}
    admitted_inputs = task.get("input_object_ids", spec.get("input_object_ids", []))
    if (not isinstance(payload, Mapping) or effect.get("effect_type") != "generation.thumbnail.attach"
            or payload.get("source_object_id") not in admitted_inputs
            or payload.get("output_name") != "thumbnail" or payload.get("output_ordinal") != 0
            or payload.get("recipe_version") != 1 or len(outputs) != 1):
        raise ValueError("thumbnail output has no exact admitted enhancement effect")
    output = outputs[0]
    if (output.get("name") != "thumbnail" or output.get("ordinal") != 0
            or output.get("media_type") != "image/jpeg" or output.get("role") != "result"):
        raise ValueError("thumbnail task must produce its one declared JPEG")
    output.update(role="thumbnail", output_port="thumbnail", durability="durable",
        provenance={"thumbnail": {"source_object_ids": [payload["source_object_id"]], "recipe_version": 1}})


def reconcile(host):
    """Admit pending durable work; extraction runs later as an ordinary task.

    No new thread or credentials are created. Calling this from the owned
    claim loop also reconciles a crash between primary settlement and admission.
    """
    supported = getattr(host.client, "supports_thumbnail_intents", None)
    if not callable(supported) or not supported():
        return
    record = host.capabilities.get(CAPABILITY_ID)
    if record is None or not record.ready:
        return
    cursor = None
    while True:
        page = host.client.pending_thumbnail_intents(executor_id=host.executor_id, cursor=cursor)
        for intent in page["items"]:
            try:
                host.client.admit_thumbnail_intent(intent["task_id"], intent["intent_id"], executor_id=host.executor_id)
            except Exception as exc:
                logging.getLogger(__name__).warning("thumbnail intent %s remains pending: %s", intent["intent_id"], exc)
        cursor = page.get("next_cursor")
        if cursor is None:
            return
