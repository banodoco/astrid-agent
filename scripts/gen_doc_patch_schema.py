"""Regenerate the canonical command schema's registered doc.patch definitions."""
from __future__ import annotations

import json
from pathlib import Path

from astrid.sdk.timeline._doc_patch_schema import extend


def main():
    path = Path(__file__).resolve().parents[1] / 'astrid/core/timeline/schema/edit-command.schema.json'
    path.write_text(json.dumps(extend(json.loads(path.read_text())), indent=2) + '\n')


if __name__ == '__main__':
    main()
