# R10 Astrid Motion test fixture

Copied in full from `astrid/packs/astrid_motion` at Astrid Agent commit
`72462b04` (Release 10), using:

```sh
git -C /home/user/astrid-agent archive 72462b04 astrid/packs/astrid_motion | tar -x -C <dest>
```

The source manifest already uses `schema_version: 3`. The adaptations are
adding `pack_api: "2.0"` and declaring `Pillow` in
`dependencies.python_requirements` for the existing preview art generator
(`preview/make_art.py` imports `PIL`). Current admission requires both the API
version and explicit third-party Python dependencies. All element
schemas, implementations, documentation and preview assets retain their R10
bytes. This README is new.

`tests/conftest.py` exposes this collection through `ASTRID_PACKS_PATH` for
SDK and editing conformance tests, including CLI subprocesses. Tests modeling
an absent or broken pack override that environment and clear registry caches.
This keeps motion coverage independent of a developer's installed packs.
