"""Closed Host browser selection; parser smoke is not a task/render witness."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import render_command_pipe as relay


class LaunchBoundary(Exception):
    pass


@pytest.fixture
def browser_command(tmp_path, monkeypatch):
    from astrid.core.rendering import remotion_runtime

    project = tmp_path / 'project'
    project.mkdir()
    cli = project / 'cli.js'
    cli.write_text('// boundary fixture; never executed')
    private = tmp_path / 'private'
    private.mkdir()
    props = private / 'props.json'
    props.write_text('{}')
    node = Path(os.environ.get('ASTRID_NODE_EXECUTABLE', sys.executable)).resolve()
    browser = tmp_path / 'host-selected-browser'
    browser.write_text('selection fixture; never executed')
    selections, launches = [], []

    def select_browser():
        selections.append(browser)
        return browser

    owner = relay.HostRenderCommandBridge.__new__(relay.HostRenderCommandBridge)
    owner.role = 'backend'
    owner.closure = SimpleNamespace(project=project, validate=lambda _: cli,
                                    require_retained_transport=lambda: None)
    owner.policy = SimpleNamespace(private_write_root=private, executable_bindings=[
        SimpleNamespace(name='node', executable=SimpleNamespace(resolved=node))],
        renderer_reads=SimpleNamespace(environment={}))
    owner.environment = {}
    owner.renderer_transport = SimpleNamespace(metadata=lambda: {'port': 17771},
                                               select_browser=select_browser)
    monkeypatch.setattr(remotion_runtime, '_validated_runtime', lambda *a, **kw: {
        'registry': {'renderer': {'observation': {'roots': [str(project)]}}}})

    def launch(args, **options):
        launches.append((list(args), options))
        raise LaunchBoundary('stop before process launch; no renderer substitute')

    owner._launch = launch
    request = {'argv': [str(node), str(cli), 'render', 'TimelineComposition',
                       '--props', str(props), '--output', str(private / 'video.mp4'),
                       '--port=17771', '--concurrency=1', '--bundle-cache=false'],
               'cwd': str(project),
               'render_environment': {k: str(private) for k in ['TMPDIR', 'TMP', 'TEMP']}}
    return owner, request, browser, selections, launches


def test_host_pinned_browser_reaches_cli_without_mutating_caller_request(browser_command):
    owner, request, browser, selections, launches = browser_command
    original = json.loads(json.dumps(request))
    with pytest.raises(LaunchBoundary):
        owner._cli(request)
    assert request == original
    assert selections == [browser] and len(launches) == 1
    command, options = launches[0]
    assert [a for a in command if a.startswith('--browser-executable=')] == [
        f'--browser-executable={browser}']
    assert options['_cli_environment']['ASTRID_REMOTION_RETAINED_BROWSER'] == str(browser)


@pytest.mark.parametrize('override', ['flag-host', 'flag-other', 'split-flag', 'private-env', 'puppeteer-env'])
def test_caller_cannot_supply_even_the_same_browser_override(browser_command, override):
    owner, request, browser, selections, launches = browser_command
    if override == 'flag-host':
        request['argv'].append(f'--browser-executable={browser}')
    elif override == 'flag-other':
        request['argv'].append('--browser-executable=/untrusted/browser')
    elif override == 'split-flag':
        request['argv'].extend(['--browser-executable', '/untrusted/browser'])
    else:
        key = 'ASTRID_REMOTION_RETAINED_BROWSER' if override == 'private-env' else 'PUPPETEER_EXECUTABLE_PATH'
        request['render_environment'][key] = '/untrusted/browser'
    with pytest.raises(ValueError, match='unsupported Remotion'):
        owner._cli(request)
    assert not selections and not launches


@pytest.mark.parametrize('remove_host_flag', [False, True])
def test_installed_cli_browser_option_prevents_auto_download(browser_command, remove_host_flag):
    """Real pinned-package parser/public ensureBrowser; no browser/render launched."""
    from astrid.core.execution.remotion_host_session import _DARWIN_BROWSER

    raw_project = os.environ.get('ASTRID_REMOTION_PROJECT_DIR')
    raw_node = os.environ.get('ASTRID_NODE_EXECUTABLE')
    if not raw_project or not raw_node or not _DARWIN_BROWSER.is_file():
        pytest.skip('requires explicitly admitted local Remotion/Node/browser fixtures')
    project = Path(raw_project)
    assert (project / 'node_modules/@remotion/cli/dist/parsed-cli.js').is_file()
    owner, request, _, _, launches = browser_command
    owner.renderer_transport.select_browser = lambda: _DARWIN_BROWSER
    with pytest.raises(LaunchBoundary):
        owner._cli(request)
    command = launches[0][0]
    if remove_host_flag:
        command = [a for a in command if not a.startswith('--browser-executable=')]
    code = r"""
const [root,raw]=process.argv.slice(1);process.argv=JSON.parse(raw);
const {parsedCli}=require(root+'/node_modules/@remotion/cli/dist/parsed-cli.js');
const {browserExecutableOption}=require(root+'/node_modules/@remotion/renderer/dist/options/browser-executable.js');
const {ensureBrowser}=require(root+'/node_modules/@remotion/renderer/dist/ensure-browser.js');
const selected=browserExecutableOption.getValue({commandLine:parsedCli});let called=false;
ensureBrowser({browserExecutable:selected.value,logLevel:'error',onBrowserDownload:()=>{
 called=true;throw new Error('unit-download-barrier-before-filesystem-or-network');
}}).then(result=>console.log(JSON.stringify({selected,result,called}))).catch(error=>{
 if(error.message!=='unit-download-barrier-before-filesystem-or-network'){console.error(error);process.exitCode=1;return;}
 console.log(JSON.stringify({selected,called,downloadBlocked:true}));
});
"""
    result = subprocess.run([raw_node, '-e', code, str(project), json.dumps(command)],
                            cwd=project, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    if remove_host_flag:
        assert observed['called'] and observed['downloadBlocked']
        assert observed['selected']['value'] is None
    else:
        assert not observed['called']
        assert observed['selected'] == {'source': 'cli', 'value': str(_DARWIN_BROWSER)}
        assert observed['result'] == {'path': str(_DARWIN_BROWSER), 'type': 'user-defined-path'}
