"""Exact historical canonical schemas for installer migration/recovery tests."""

def remove_round1b_schema_objects(connection):
    """Undo U33-only structure before the existing v29 fixture downgrades.

    Changing only runtime_schema.version would leave a hybrid inventory, which
    the published producer correctly refuses. Keep the old fixture's data and
    version assertions while removing every later schema object.
    """
    connection.execute("DROP INDEX idx_generations_gen")
    for table in ("timeline_document_input_links", "timeline_document_input_bindings",
                  "timeline_document_input_contents", "timeline_commit_history"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("ALTER TABLE parent_composition_heads DROP COLUMN publication_id")
    connection.execute("ALTER TABLE parent_composition_revisions DROP COLUMN commit_manifest_json")


def register_timeline_provider(realm):
    """Give the simulated daemon the real candidate provider startup barrier."""
    from astrid.sdk.timeline.commit import create_runtime_commit_service
    from runtime_protocol.service import RuntimeService

    service = RuntimeService(realm)
    try:
        service.register_timeline_commit_provider(create_runtime_commit_service())
    finally:
        service.close()
