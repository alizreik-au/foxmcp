"""Exercise the real Windows worker launcher, including the venv child."""

import json
import os
import subprocess
import sys
import time

import pytest

import test_imports
from server.single_instance import spawn_windows_worker


@pytest.mark.skipif(os.name != 'nt', reason='Windows WMI worker launcher')
def test_worker_is_hidden_and_preserves_environment(tmp_path, monkeypatch):
    probe = tmp_path / 'worker probe.py'
    receipt = tmp_path / 'worker.json'
    stop = tmp_path / 'stop'
    log = tmp_path / 'worker.log'
    monkeypatch.setenv('FOXMCP_WINDOW_PROBE', 'preserved value')
    probe.write_text(
        'import json, os, pathlib, sys, time\n'
        'receipt, stop = map(pathlib.Path, sys.argv[1:])\n'
        'print("worker logging works", file=sys.stderr, flush=True)\n'
        'receipt.write_text(json.dumps({"pid": os.getpid(), '
        '"env": os.environ.get("FOXMCP_WINDOW_PROBE")}))\n'
        'deadline = time.monotonic() + 15\n'
        'while not stop.exists() and time.monotonic() < deadline: time.sleep(.1)\n',
        encoding='utf-8',
    )
    broker = spawn_windows_worker([sys.executable, str(probe), str(receipt), str(stop)], log)
    try:
        deadline = time.monotonic() + 15
        while not receipt.exists() and time.monotonic() < deadline:
            assert broker.poll() is None, log.read_text()
            time.sleep(.1)
        data = json.loads(receipt.read_text())
        assert data['env'] == 'preserved value'
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
             f"(Get-Process -Id {int(data['pid'])}).MainWindowHandle.ToInt64()"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '0', 'The worker opened a visible window'
        assert 'worker logging works' in log.read_text()
    finally:
        stop.touch()
        broker.wait(timeout=20)
    assert broker.returncode == 0
