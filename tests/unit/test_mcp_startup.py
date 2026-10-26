"""Port selection and startup diagnostics without touching the live server's ports."""

import asyncio
import errno
import logging
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from server import server as server_module
from server.server import FoxMCPServer, find_available_port


# Keep the real constructor so the test fixture can't replace the default ports.
SERVER_INIT = FoxMCPServer.__init__
TEST_PORT = 30600


@pytest.fixture(autouse=True)
def production_constructor(monkeypatch):
    monkeypatch.setattr(FoxMCPServer, '__init__', SERVER_INIT)


@pytest.fixture
def probes(monkeypatch):
    """Model OS bind errors, including IPv6 on hosts where it is disabled."""
    state = SimpleNamespace(errors={}, creation_errors={}, sockets=[])
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', TEST_PORT)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', TEST_PORT, 0, 0)),
    ]

    def create(family, socktype, proto):
        if family in state.creation_errors:
            raise OSError(state.creation_errors[family], 'socket unavailable')
        sock = MagicMock()
        sock.__enter__.return_value = sock

        def bind(address):
            error = state.errors.get((family, address[1]))
            if error:
                raise OSError(error, 'bind failed')

        sock.bind.side_effect = bind
        state.sockets.append(sock)
        return sock

    state.resolve = Mock(return_value=addresses)
    monkeypatch.setattr(server_module.socket, 'getaddrinfo', state.resolve)
    monkeypatch.setattr(server_module.socket, 'socket', create)
    return state


def test_available_preferred_port(probes):
    assert find_available_port(TEST_PORT) == TEST_PORT
    assert len(probes.sockets) == 2
    for sock in probes.sockets:
        sock.__exit__.assert_called_once()


@pytest.mark.parametrize('family', [socket.AF_INET, socket.AF_INET6])
def test_conflict_on_either_family_moves_to_next_port(probes, family):
    probes.errors[family, TEST_PORT] = errno.EADDRINUSE
    assert find_available_port(TEST_PORT) == TEST_PORT + 1
    for sock in probes.sockets:
        sock.__exit__.assert_called_once()


def test_skips_consecutive_conflicts(probes):
    probes.errors[socket.AF_INET, TEST_PORT] = errno.EADDRINUSE
    probes.errors[socket.AF_INET6, TEST_PORT + 1] = errno.EADDRINUSE
    assert find_available_port(TEST_PORT) == TEST_PORT + 2


@pytest.mark.parametrize('start,attempts', [(TEST_PORT, 100), (65535, 100)])
def test_exhaustion_is_bounded_by_attempts_and_valid_ports(probes, start, attempts):
    last = min(start + attempts - 1, 65535)
    for port in range(start, last + 1):
        probes.errors[socket.AF_INET, port] = errno.EADDRINUSE
    with pytest.raises(RuntimeError, match=f'{start}-{last}.*--mcp-port'):
        find_available_port(start, attempts)
    bound_ports = {s.bind.call_args.args[0][1] for s in probes.sockets}
    assert bound_ports == set(range(start, last + 1))
    for sock in probes.sockets:
        sock.__exit__.assert_called_once()


def test_explicit_ipv4_host_only_checks_its_addresses(probes):
    probes.resolve.return_value = probes.resolve.return_value[:1]
    probes.errors[socket.AF_INET6, TEST_PORT] = errno.EADDRINUSE
    assert find_available_port(TEST_PORT, host='127.0.0.1') == TEST_PORT
    assert probes.resolve.call_args.args[0] == '127.0.0.1'
    assert len(probes.sockets) == 1


@pytest.mark.parametrize('error', [errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT])
def test_unavailable_ipv6_socket_does_not_block_ipv4(probes, error):
    probes.creation_errors[socket.AF_INET6] = error
    assert find_available_port(TEST_PORT) == TEST_PORT


def test_resolved_but_disabled_ipv6_address_does_not_block_ipv4(probes):
    probes.errors[socket.AF_INET6, TEST_PORT] = errno.EADDRNOTAVAIL
    assert find_available_port(TEST_PORT) == TEST_PORT


def test_no_usable_address_is_an_error(probes):
    for family in (socket.AF_INET, socket.AF_INET6):
        probes.errors[family, TEST_PORT] = errno.EADDRNOTAVAIL
    with pytest.raises(OSError, match='Could not bind to any address'):
        find_available_port(TEST_PORT)


def test_permission_error_is_not_reported_as_a_conflict(probes):
    probes.errors[socket.AF_INET, TEST_PORT] = errno.EACCES
    with pytest.raises(OSError) as caught:
        find_available_port(TEST_PORT)
    assert caught.value.errno == errno.EACCES


@pytest.mark.parametrize('port', [-1, 0, 65536])
def test_invalid_port_has_actionable_error(port):
    with pytest.raises(ValueError, match='between 1 and 65535'):
        find_available_port(port)


@pytest.mark.parametrize('requested', [None, TEST_PORT])
def test_default_and_explicit_ports_use_the_same_selection(monkeypatch, caplog, requested):
    selector = Mock(return_value=TEST_PORT + 1)
    monkeypatch.setattr(server_module, 'find_available_port', selector)
    server = FoxMCPServer(host='127.0.0.1', mcp_port=requested)
    preferred = 3000 if requested is None else requested
    selector.assert_called_once_with(preferred, host='127.0.0.1')
    assert server.mcp_port == TEST_PORT + 1
    assert f'MCP port {preferred} is unavailable; selected port {TEST_PORT + 1}' in caplog.text


@pytest.mark.parametrize('options', [{'use_stdio': True}, {'start_mcp': False}])
def test_no_http_listener_needs_no_port(monkeypatch, options):
    selector = Mock(side_effect=AssertionError('No HTTP port should be checked'))
    monkeypatch.setattr(server_module, 'find_available_port', selector)
    server = FoxMCPServer(**options)
    assert server.mcp_port is None
    selector.assert_not_called()


@pytest.fixture
def startup(monkeypatch):
    monkeypatch.setattr(server_module, 'find_available_port', Mock(return_value=TEST_PORT))
    server = FoxMCPServer(mcp_port=TEST_PORT)
    websocket = Mock()
    websocket.wait_closed = AsyncMock()
    server.websocket_server = websocket
    server.start_browser_listener = AsyncMock()
    worker = SimpleNamespace(started=False, should_exit=False)

    def run():
        while not worker.should_exit:
            time.sleep(0.005)

    worker.run = run
    monkeypatch.setattr(server_module.uvicorn, 'Server', Mock(return_value=worker))
    return server, worker, websocket


@pytest.mark.asyncio
async def test_success_is_announced_only_when_worker_is_ready(startup, caplog):
    server, worker, _ = startup
    server._preferred_mcp_port = TEST_PORT - 1
    caplog.set_level(logging.INFO)
    task = asyncio.create_task(server.start_server())
    try:
        await asyncio.sleep(0.01)
        assert not task.done()
        assert 'MCP tools available' not in caplog.text
        worker.started = True
        await asyncio.wait_for(task, 1)
        url = f'http://localhost:{TEST_PORT}/mcp/'
        assert f'MCP tools available at {url}' in caplog.text
        assert f"Update your MCP client's URL to {url}" in caplog.text
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server._stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('broken startup'), SystemExit(1), None])
async def test_worker_failure_cleans_up_without_announcing_success(startup, caplog, error):
    server, worker, websocket = startup

    def fail():
        if error is not None:
            raise error

    worker.run = fail
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError, match='MCP server failed to start') as caught:
        await server.start_server()
    assert caught.value.__cause__ is error
    assert 'MCP tools available' not in caplog.text
    assert server.mcp_thread is None
    websocket.close.assert_called_once()
    websocket.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_error_after_successful_startup_is_still_logged(startup, caplog):
    server, worker, _ = startup
    fail = threading.Event()

    def run():
        worker.started = True
        fail.wait()
        raise RuntimeError('failed after binding')

    worker.run = run
    try:
        await server.start_mcp_server()
        fail.set()
        await asyncio.to_thread(server.mcp_thread.join, 1)
        assert 'failed after binding' in caplog.text
    finally:
        fail.set()
        await server._stop()


@pytest.mark.asyncio
async def test_startup_timeout_stops_worker_and_releases_browser_listener(startup, caplog):
    server, worker, websocket = startup
    server.MCP_STARTUP_TIMEOUT = 0.01
    caplog.set_level(logging.INFO)
    with pytest.raises(TimeoutError, match='did not start within'):
        await server.start_server()
    assert worker.should_exit
    assert server.mcp_thread is None
    assert 'MCP tools available' not in caplog.text
    websocket.close.assert_called_once()
    websocket.wait_closed.assert_awaited_once()
