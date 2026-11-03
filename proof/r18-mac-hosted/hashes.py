import hashlib
from pathlib import Path
root = Path('out')
paths = sorted(p for p in root.rglob('*') if p.is_file() and p.name != 'SHA256SUMS')
(root/'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root)}\n' for p in paths))
