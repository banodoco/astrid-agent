"""Small data/path helpers, with no ambient configuration or product imports."""
import hashlib
import json
from pathlib import Path


class GateError(RuntimeError):
    pass


def require(condition, stage, path, detail):
    if not condition:
        raise GateError(f"stage={stage} path={path}: {detail}")


def exact_path(value, *, kind=None):
    require(isinstance(value, str) and value, 'inputs', value, 'absolute path required')
    path = Path(value)
    require(path.is_absolute() and '..' not in path.parts and str(path) == value,
            'inputs', value, 'canonical absolute path required')
    require(not any(p.is_symlink() for p in (path, *path.parents)),
            'inputs', path, 'path alias refused')
    if kind:
        require(path.is_file() if kind == 'file' else path.is_dir(),
                'inputs', path, f'existing {kind} required')
    return path


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise GateError(f'stage=read path={path}: {exc}') from exc


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root):
    root = exact_path(str(root), kind='directory')
    files = {}
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'inventory', path, 'alias refused')
        if '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo'}:
            continue
        if path.is_file():
            files[path.relative_to(root).as_posix()] = sha(path)
    require(bool(files), 'inventory', root, 'empty inventory')
    return files


def pages(fetch):
    """Fetch every page, including empty intermediate pages, refusing cursor cycles."""
    cursor = None
    seen = set()
    result = []
    while True:
        rows, cursor = fetch(cursor=cursor, limit=100)
        result.extend(rows)
        if cursor is None:
            return result
        require(isinstance(cursor, str) and cursor not in seen,
                'pagination', cursor, 'invalid or repeated cursor')
        seen.add(cursor)
