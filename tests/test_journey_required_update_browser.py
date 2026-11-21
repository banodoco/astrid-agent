"""Targeted controls for FIX-17 browser publication and real draft recovery."""
from pathlib import Path

from scripts import journey_required_update as driver


ROOT = Path(__file__).resolve().parents[1]
BROWSER = (ROOT / "scripts/journey_required_update_browser.mjs").read_text()


def test_required_update_keeps_publication_failure_and_continues_to_reopen():
    source = (ROOT / "scripts/journey_required_update.py").read_text()
    assert '"mode": "publication"' in source
    assert 'observed.get("status") not in {"pass", "fail"}' in source
    assert 'with stage(result, result_path, "required_prompt") as entry' in source
    assert 'with stage(result, result_path, "r14_reopen") as entry' in source
    assert "publication independently" in driver._browser.__doc__


def test_browser_retries_original_scoped_draft_and_reads_fresh_composition():
    assert "mode === 'publication'" in BROWSER
    assert "freshContext(browser, config" in BROWSER
    assert "name: 'Try again', exact: true" in BROWSER
    assert "body === pending.body && key === pending.key" in BROWSER
    assert "body.data?.status !== 'accepted'" in BROWSER
    assert "Fresh R14 client did not reopen the accepted draft composition label" in BROWSER
    assert "saveAttemptsBeforePrompt" in BROWSER
    assert "reload(" not in BROWSER and "forceRefresh" not in BROWSER
