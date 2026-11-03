import hashlib, json, urllib.request, urllib.error
from pathlib import Path
root=Path(__file__).resolve().parent
revision='efc2276d30c22ac0cbe54f735a1129cef79583ef'
url=f'https://raw.githubusercontent.com/banodoco/reigh-worker/{revision}/source/runtime/supervisor.py'
try:
    with urllib.request.urlopen(url, timeout=30) as response:
        body=response.read(2*1024*1024+1)
        if len(body)>2*1024*1024: raise RuntimeError('source exceeds inspection bound')
        (root/'evidence'/'legacy-supervisor.py').write_bytes(body)
        print(json.dumps({'url':url,'revision':revision,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(), 'prepared_control_option_present':b'--prepared-control-fd' in body,'status':response.status}))
except urllib.error.HTTPError as error:
    print(json.dumps({'url':url,'revision':revision,'http_status':error.code})); raise SystemExit(1)
