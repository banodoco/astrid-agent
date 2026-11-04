This SQLite realm was generated offline with Runtime commit
`6d53a19070faf63fc15ae1f81417722edc2c8a5f`, the Runtime pinned by the
checked-in release-0 fixture. `RealmStore.initialize`, `RuntimeService.create_project`,
and the R0 service's `create_timeline_document` produced the database and
legacy `timeline.composition` row. The accompanying JSON records its exact
row and database SHA-256. The migration test copies the database, runs the
selected Runtime's canonical migration, and compares the actual legacy row.
This is separate from the live canonical composition journey.
