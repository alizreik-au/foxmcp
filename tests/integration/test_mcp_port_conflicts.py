"""Real HTTP startup on isolated ports, including a conflict after the probe."""

import asyncio
from pathlib import Path
import re
import socket
import sys

from fastmcp import Client
import pytest

from port_coordinator import get_port_by_type


ROOT = Path(__file__).resolve().parents[2]
MCP_PORT = get_port_by_type('test_mcp_individual')
WEBSOCKET_PORT = get_port_by_type('test_individual')


@pytest.fixture
def occupied_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', MCP_PORT))
        listener.listen(1)
        yield listener


async def stop(process):
    if process.returncode is None:
        process.terminate()
    try:
        return await asyncio.wait_for(process.communicate(), 10)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise


@pytest.mark.asyncio
async def test_conflict_prints_a_working_mcp_client_url(occupied_port):
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / 'server/server.py'),
        '--port', str(WEBSOCKET_PORT), '--mcp-port', str(MCP_PORT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=ROOT,
    )
    lines = []
    try:
        async def read_url():
            while True:
                line = (await process.stderr.readline()).decode()
                assert line, ''.join(lines)
                lines.append(line)
                match = re.search(r"Update your MCP client's URL to (http://\S+)", line)
                if match:
                    return match.group(1)

        url = await asyncio.wait_for(read_url(), 30)
        selected = int(url.split(':')[2].split('/')[0])
        assert MCP_PORT < selected < MCP_PORT + 100
        assert f'MCP tools available at {url}' in ''.join(lines)
        assert f'MCP port {MCP_PORT} is unavailable' in ''.join(lines)
        async with Client(url, timeout=10) as client:
            tools = await client.list_tools()
            assert 'tabs_list' in {tool.name for tool in tools}
        # The other service's socket remains owned by the test.
        assert occupied_port.getsockname()[1] == MCP_PORT
    finally:
        await stop(process)


@pytest.mark.asyncio
async def test_bind_race_exits_and_releases_websocket_port(occupied_port):
    # Force the probe to pick an occupied port, as if another process took it
    # before Uvicorn could bind.
    script = (
        'from server import server\n'
        'server.find_available_port = lambda start_port, **kwargs: start_port\n'
        'server.run()\n'
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', script,
        '--host', '127.0.0.1', '--port', str(WEBSOCKET_PORT),
        '--mcp-port', str(MCP_PORT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=ROOT,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), 30)
        log = stderr.decode()
        assert process.returncode == 1, log
        assert 'MCP server failed to start' in log
        assert 'MCP tools available' not in log
        assert '--mcp-port' in log
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', WEBSOCKET_PORT))
    finally:
        await stop(process)
