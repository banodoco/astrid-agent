BUG-14 CPU evidence copied from the DEEP-BUG14 repro fixtures.

`qwen_image_2_1_turbo.py` is the exact 4,186-byte submitted source used by
`sub/2/cpu_regression.py`: SHA-256
`a47e24e6da4dfb7a41413e563fa90dd4e2d45a49a423ee132e5c00cbc27371b9`.
Ingest it as `qwen_image_2_1_turbo.py` / `text/x-python`, then publish the
same bytes as `evidence-source-source.json` / `application/json` to reproduce
the pinned Runtime's deterministic conflict. The integration test reuses
`../comfyui/live-423cbd20/history.json` for the completed producer fixture.
No provider submission or GPU is needed.

`expired-task.json` retains only the existing Runtime observation fields from
the live task used by `cpu/repro_cli_wait.py` and `cpu/repro_expiry.py`'s custody
scenario. The waiter tests freeze wall time at 2026-11-01 16:29 UTC and advance
only their virtual clock; they do not sleep or contact a service.
