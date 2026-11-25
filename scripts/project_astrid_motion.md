# Motion pack projection (RN-5b-min)

The one projection is generated from Git objects at the reviewed upstream commit, not from a working tree or the old poms pack. It changes exactly four named imports and inserts two manifest declarations (pack_api and dependencies.python_requirements). The upstream digest is provenance, not installed identity. The independent product witness is `astrid/resources/astrid_motion_projection.json`; it is not inside the projected pack. No derivative Git commit, built-in pack or synthetic PIN is created.

From the Agent checkout containing this tool, with the historical source commit available in a Git checkout:

```sh
mkdir -p /tmp/astrid-motion-source
python scripts/project_astrid_motion.py --upstream-repo /path/to/agent --output /tmp/astrid-motion-source/astrid_motion
python scripts/project_astrid_motion.py --check --output /tmp/astrid-motion-source/astrid_motion
astrid packs add /tmp/astrid-motion-source/astrid_motion --pack-id astrid_motion
```

Use a new disposable source directory and an isolated user collection for proof. The collection must contain only the installed motion pack, not a copied sibling `rendering/` pack (the built-in rendering pack is already discovered). The named alias resolves against the built-in rendering pack. Never set a built-in pack directory as a collection root. The manual folder install's existing unpinned PIN is not overridden. Check installed bytes against the independently shipped witness; a pack's own metadata cannot authorize its identity.

For browser vendoring, consume the exact generated tree, not a second copy with manually changed imports. The existing upstream commit field remains provenance; the additive `astrid_motion_tree` editor pack revision string carries the bare lowercase 64-character projection SHA-256. The same pack-tree-v1 digest is the runtime identity. Element descriptor hashes remain the product's per-element content hashes, not the pack digest.

This tool does not install packs, change release manifests/pins, modify installer behavior, or qualify the installed render. RN-14 remains deferred to Release 16.
