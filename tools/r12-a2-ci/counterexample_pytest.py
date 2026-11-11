"""Local isolated pytest format counterexample; prints only fixed diagnostic data."""
import os, pathlib, subprocess, sys, tempfile
from sanitize import sanitized_result

with tempfile.TemporaryDirectory(prefix='a2-pytest-format-') as tmp:
    root=pathlib.Path(tmp); (root/'tests').mkdir()
    (root/'tests/test_installer_r0_entry.py').write_text('''import pytest
@pytest.fixture
def cleanup_fails():
    yield
    raise RuntimeError("synthetic teardown")
def test_teardown_counterexample(cleanup_fails):
    pass
def test_captures_match_real_r0_evidence():
    pytest.skip("local R0 evidence is not present")
''')
    env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':tmp,'TMPDIR':tmp,'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'}
    cmd=[sys.executable,'-m','pytest','-vv','--tb=line','-rA','-p','no:cacheprovider','tests/test_installer_r0_entry.py']
    proc=subprocess.run(cmd,cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=30)
    a='tests/test_installer_r0_entry.py::test_teardown_counterexample'
    b='tests/test_installer_r0_entry.py::test_captures_match_real_r0_evidence'
    receipt=sanitized_result(proc.stdout,[a,b],[],{},proc.returncode,True)
    if proc.returncode != 1 or receipt['classification'] != 'COMPLETE' or receipt['additional_error_after_outcome_ids'] != [a] or receipt['outcome_counts'] != {'ERROR':1,'PASSED':1,'SKIPPED':1}:
        print('PYTEST_FORMAT_COUNTEREXAMPLE failed',flush=True); sys.exit(1)
    print('PYTEST_FORMAT_COUNTEREXAMPLE passed; 2 IDs, pass + teardown error, known skip; exit 1',flush=True)
    # Add a warning after pytest's initial warning summary, exercising its exact
    # native "warnings summary (final)" heading without exposing warning text.
    (root/'conftest.py').write_text('''import warnings
def pytest_terminal_summary(terminalreporter):
    message = warnings.WarningMessage(UserWarning("synthetic late warning"), UserWarning, __file__, 1, None, None)
    terminalreporter.pytest_warning_recorded(message, "")
''')
    (root/'tests/test_summary_format.py').write_text('''import warnings
def test_synthetic_failure():
    warnings.warn("synthetic early warning", UserWarning)
    assert False
''')
    cmd=[sys.executable,'-m','pytest','-vv','--tb=line','-rA','-p','no:cacheprovider','tests/test_summary_format.py']
    proc=subprocess.run(cmd,cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=30)
    marker=b'warnings summary (final)'
    node='tests/test_summary_format.py::test_synthetic_failure'
    receipt=sanitized_result(proc.stdout,[node],[],{},proc.returncode,True)
    if proc.returncode != 1 or marker not in proc.stdout or receipt['classification'] != 'COMPLETE' or receipt['outcome_counts'] != {'FAILED':1}:
        print('PYTEST_WARNING_COUNTEREXAMPLE failed', 'exit='+str(proc.returncode), 'final_heading='+str(marker in proc.stdout), 'classification='+receipt['classification'], 'outcomes='+str(receipt['outcome_counts']), 'reasons='+','.join(receipt['incomplete_reasons']),flush=True); sys.exit(1)
    print('PYTEST_WARNING_COUNTEREXAMPLE passed; 1 failed ID, native final-warning heading; exit 1',flush=True)
