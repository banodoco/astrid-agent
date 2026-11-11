"""Fixed-enum progress signals: never format exception or subprocess text."""
import json, subprocess

STAGES = ('input_validation', 'checkout_verification', 'source_fetch', 'pin_verification',
          'dependency_setup', 'runtime_preflight', 'toolchain_preflight', 'selection', 'pytest')
EVENTS = ('entered', 'passed', 'failed')

def validate_events(events):
    safe = []
    if not isinstance(events, list): raise ValueError('invalid events')
    for item in events:
        if not isinstance(item, dict) or set(item) != {'stage','event','exit_code'}: raise ValueError('invalid event structure')
        stage, event, code = item['stage'], item['event'], item['exit_code']
        if stage not in STAGES or event not in EVENTS: raise ValueError('invalid event enum')
        if code is not None and (type(code) is not int or not -255 <= code <= 255): raise ValueError('invalid exit code')
        if event == 'entered' and code is not None: raise ValueError('entered exit code')
        safe.append({'stage':stage,'event':event,'exit_code':code})
    return safe

class StageTracker:
    def __init__(self): self.events = []; self.active = None; self.index = -1
    def _emit(self, stage, event, code=None):
        item = validate_events([{'stage':stage,'event':event,'exit_code':code}])[0]
        self.events.append(item)
        print('A2_STAGE '+json.dumps(item,sort_keys=True,separators=(',',':')),flush=True)
    def enter(self, stage):
        if stage not in STAGES or STAGES.index(stage) <= self.index: raise ValueError('invalid stage sequence')
        if self.active is not None: self.finish(True)
        self.index = STAGES.index(stage); self.active = stage; self._emit(stage,'entered')
    def finish(self, passed, code=None):
        if self.active is None or type(passed) is not bool: raise ValueError('no active stage')
        self._emit(self.active,'passed' if passed else 'failed',code); self.active = None
    def exception(self, exc):
        code = exc.returncode if isinstance(exc, subprocess.CalledProcessError) and type(exc.returncode) is int and -255 <= exc.returncode <= 255 else None
        if self.active is not None: self.finish(False,code)
