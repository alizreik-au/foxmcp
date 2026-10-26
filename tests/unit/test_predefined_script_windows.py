"""Windows interpreter selection plus real subprocess/argument regression tests."""

import json
from pathlib import Path
import sys

import pytest

import test_imports  # Automatic path setup
from server import mcp_tools


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.delenv('FOXMCP_BASH', raising=False)


def test_python_uses_server_interpreter(windows):
    path = r'C:\scripts with spaces\helper.PY'
    assert mcp_tools.predefined_script_command(path) == [sys.executable, path]


def test_posix_keeps_shebang_execution(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert mcp_tools.predefined_script_command('/scripts/a.py') == ['/scripts/a.py']


def test_native_executable_is_unchanged(windows):
    assert mcp_tools.predefined_script_command('helper.exe') == ['helper.exe']


def test_bash_is_found_beside_git_not_wsl(windows, monkeypatch, tmp_path):
    git = tmp_path / 'Git with spaces' / 'cmd' / 'git.exe'
    bash = git.parent.parent / 'bin' / 'bash.exe'
    bash.parent.mkdir(parents=True)
    bash.touch()
    monkeypatch.setattr(mcp_tools.shutil, 'which', lambda name: str(git) if name == 'git' else r'C:\Windows\System32\bash.exe')
    assert mcp_tools.predefined_script_command(r'C:\my scripts\a.sh') == [str(bash), '--', 'C:/my scripts/a.sh']


def test_explicit_bash_path(windows, monkeypatch, tmp_path):
    bash = tmp_path / 'custom bash.exe'
    bash.touch()
    monkeypatch.setenv('FOXMCP_BASH', str(bash))
    assert mcp_tools.predefined_script_command('a.sh') == [str(bash), '--', 'a.sh']


def test_invalid_override_is_actionable(windows, monkeypatch):
    monkeypatch.setenv('FOXMCP_BASH', 'bash --login')
    with pytest.raises(ValueError, match='absolute path'):
        mcp_tools.predefined_script_command('a.sh')


def test_missing_bash_is_actionable(windows, monkeypatch):
    monkeypatch.setattr(mcp_tools.shutil, 'which', lambda _: None)
    for variable in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ValueError, match='Install Git for Windows'):
        mcp_tools.predefined_script_command('a.sh')


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows process test')
@pytest.mark.parametrize('extension', ['py', 'sh'])
def test_real_process_preserves_arguments(tmp_path, extension):
    directory = tmp_path / 'scripts with spaces'
    directory.mkdir()
    script = directory / f'echo-args.{extension}'
    arguments = ['two words', '"quoted"', "it's literal", '$HOME; & | > < $(echo injected)', '', '--option', 'line1\nline2']
    if extension == 'py':
        body = 'import json, sys\nprint(json.dumps(sys.argv[1:]))\n'
    else:
        body = '#!/bin/bash\nprintf "%s\\0" "$@"\n'
    script.write_text(body, encoding='utf-8', newline='\n')
    try:
        command = mcp_tools.predefined_script_command(str(script))
    except ValueError as error:
        pytest.skip(str(error))
    result = mcp_tools.run_predefined_script(command, arguments)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout) if extension == 'py' else result.stdout.split('\0')[:-1]
    assert actual == arguments


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows process test')
@pytest.mark.asyncio
@pytest.mark.parametrize('name,args,marker', [
    ('set-file-input.py', ['example-file', '#upload'], 'DataTransfer'),
    ('dom-summarize.sh', ['onscreen', 'withpos'], 'traverseDOM'),
])
async def test_bundled_script_reaches_browser_transport(monkeypatch, name, args, marker):
    class Browser:
        sent = None

        async def send_request_and_wait(self, request):
            self.sent = request
            return {'type': 'response', 'data': {'result': 'browser result', 'url': 'http://localhost/'}}

    scripts = Path(__file__).resolve().parents[2] / 'predefined-ex'
    monkeypatch.setenv('FOXMCP_EXT_SCRIPTS', str(scripts))
    browser = Browser()
    tool = await mcp_tools.FoxMCPTools(browser).mcp.get_tool('content_execute_predefined')
    result = await tool.fn(tab_id=42, script_name=name, script_args=json.dumps(args))
    assert 'browser result' in result, result
    assert browser.sent['data']['tabId'] == 42
    assert marker in browser.sent['data']['script']
