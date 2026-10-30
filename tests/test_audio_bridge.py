"""Audio-bridge helpers and the spoken Gladia failure path, without a robot."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from queue import Full, Queue
from unittest.mock import patch

BRIDGE_DIR = Path(__file__).resolve().parents[1]
PACKAGING_DIR = BRIDGE_DIR / "packaging"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import audio_bridge
import register_autoload
from SimpleWebSocketServer.SimpleWebSocketServer import (
    WebSocket,
    as_socket_buffer,
    is_would_block,
    prepare_client_socket,
    socket_error_code,
)

from nao_websocket_bridge_protocol import websocket_url as audio_bridge_websocket_url


class _Client:
    def __init__(self, host: str) -> None:
        self.address = (host, 1)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class AudioBridgeHelperTests(unittest.TestCase):
    def test_websocket_url_appends_a_token_path(self) -> None:
        self.assertEqual(
            audio_bridge_websocket_url("10.0.0.5", 8765, ""),
            "ws://10.0.0.5:8765",
        )
        self.assertEqual(
            audio_bridge_websocket_url("10.0.0.5", 8765, " booth-secret/ "),
            "ws://10.0.0.5:8765/booth-secret",
        )

    def test_stale_service_ids_keep_only_the_bridge_name(self) -> None:
        services = [
            {"name": "ALAudioDevice", "serviceId": 1},
            {"name": "NaoAudioBridge", "serviceId": 7},
            {"name": "NaoAudioBridge", "id": 8},
            {"name": "Other", "serviceId": 9},
            {"name": "NaoAudioBridge"},
        ]

        self.assertEqual(
            audio_bridge.stale_service_ids(services, "NaoAudioBridge"),
            [7, 8],
        )

    def test_a_different_machine_cannot_replace_a_live_client(self) -> None:
        state = audio_bridge.BridgeState("")
        first = _Client("192.168.2.75")
        second = _Client("192.168.2.24")
        restarted = _Client("192.168.2.75")

        self.assertTrue(state.add_client(first))
        self.assertFalse(state.add_client(second))
        self.assertTrue(second.closed)
        self.assertEqual(state.clients, [first])

        self.assertTrue(state.add_client(restarted))
        self.assertTrue(first.closed)
        self.assertEqual(state.clients, [restarted])

    def test_path_token_is_optional_and_exact(self) -> None:
        self.assertTrue(audio_bridge.path_is_authorized("/anything", ""))
        self.assertTrue(audio_bridge.path_is_authorized(None, ""))
        self.assertTrue(audio_bridge.path_is_authorized("/secret", "secret"))
        self.assertTrue(audio_bridge.path_is_authorized("/secret/", "secret"))
        self.assertTrue(audio_bridge.path_is_authorized("/secret?x=1", "secret"))
        self.assertFalse(audio_bridge.path_is_authorized("/other", "secret"))
        self.assertFalse(audio_bridge.path_is_authorized("", "secret"))
        self.assertFalse(audio_bridge.path_is_authorized("/secret/extra", "secret"))

    def test_trim_sendq_drops_the_oldest_pcm_frames(self) -> None:
        client = type("Client", (), {})()
        client.send_partial = False
        client.sendq = deque([
            (audio_bridge._WS_BINARY, b"a"),
            (audio_bridge._WS_BINARY, b"b"),
            (audio_bridge._WS_BINARY, b"c"),
            (audio_bridge._WS_BINARY, b"d"),
            (0x1, b"status"),
        ])

        dropped = audio_bridge.trim_sendq(client, max_frames=3)

        self.assertEqual(dropped, 2)
        self.assertEqual(
            list(client.sendq),
            [
                (audio_bridge._WS_BINARY, b"c"),
                (audio_bridge._WS_BINARY, b"d"),
                (0x1, b"status"),
            ],
        )

    def test_trim_sendq_keeps_a_partially_sent_head(self) -> None:
        client = type("Client", (), {})()
        client.send_partial = True
        client.sendq = deque(
            [
                (audio_bridge._WS_BINARY, b"partial"),
                (audio_bridge._WS_BINARY, b"old"),
                (audio_bridge._WS_BINARY, b"new"),
            ]
        )

        dropped = audio_bridge.trim_sendq(client, max_frames=2)

        self.assertEqual(dropped, 1)
        self.assertEqual(
            list(client.sendq),
            [
                (audio_bridge._WS_BINARY, b"partial"),
                (audio_bridge._WS_BINARY, b"new"),
            ],
        )

    def test_queue_client_message_keeps_only_the_newest_frames(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.sendq: deque[tuple[int, bytes]] = deque()
                self.send_partial = False

            def sendMessage(self, data: bytes) -> None:
                self.sendq.append((audio_bridge._WS_BINARY, data))

        client = Client()
        discarded = [
            audio_bridge.queue_client_message(client, bytes([index]), max_frames=3)
            for index in range(5)
        ]

        self.assertEqual(discarded, [0, 0, 0, 1, 1])
        self.assertEqual(
            [payload for _opcode, payload in client.sendq],
            [b"\x02", b"\x03", b"\x04"],
        )

    def test_send_stall_requires_a_quiet_queue(self) -> None:
        client = type("Client", (), {})()
        client.sendq = deque([(audio_bridge._WS_BINARY, b"pcm")])
        client.last_send_progress_at = 10.0
        client.connected_at = 0.0

        self.assertFalse(
            audio_bridge.send_is_stalled(client, 10.0 + audio_bridge.SEND_STALL_SECS - 0.1)
        )
        self.assertTrue(
            audio_bridge.send_is_stalled(client, 10.0 + audio_bridge.SEND_STALL_SECS)
        )

        client.sendq.clear()
        self.assertFalse(
            audio_bridge.send_is_stalled(client, 10.0 + audio_bridge.SEND_STALL_SECS)
        )

    def test_send_stall_uses_connect_time_before_the_first_byte(self) -> None:
        client = type("Client", (), {})()
        client.sendq = deque([(audio_bridge._WS_BINARY, b"pcm")])
        client.last_send_progress_at = None
        client.connected_at = 4.0

        self.assertFalse(audio_bridge.send_is_stalled(client, 4.0 + 1.0))
        self.assertTrue(
            audio_bridge.send_is_stalled(client, 4.0 + audio_bridge.SEND_STALL_SECS)
        )

    def test_enqueue_pcm_drops_the_oldest_chunk(self) -> None:
        audio_queue: Queue[bytes] = Queue(maxsize=1)
        audio_queue.put_nowait(b"old")

        dropped = audio_bridge.enqueue_pcm(audio_queue, b"new")

        self.assertEqual(dropped, 1)
        self.assertEqual(audio_queue.get_nowait(), b"new")
        self.assertTrue(audio_queue.empty())

    def test_enqueue_pcm_counts_a_chunk_that_cannot_be_queued(self) -> None:
        class StuckQueue:
            def put_nowait(self, _pcm: bytes) -> None:
                raise Full

            def get_nowait(self) -> bytes:
                return b"old"

        self.assertEqual(audio_bridge.enqueue_pcm(StuckQueue(), b"new"), 2)

    def test_status_message_shape(self) -> None:
        payload = json.loads(audio_bridge.status_message(4, True))

        self.assertEqual(
            payload,
            {"type": "status", "dropped_chunks": 4, "subscribed": True},
        )

    def test_qi_application_is_constructed_once(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.connected = False

            def isConnected(self) -> bool:
                return self.connected

            def connect(self, url: str) -> None:
                self.connected = True

        class App:
            def __init__(self) -> None:
                self.session = Session()

            def start(self) -> None:
                return None

        calls = {"n": 0}

        def Application(args):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("Application was already initialized")
            return App()

        class FakeQi:
            pass

        FakeQi.Application = staticmethod(Application)

        old = (audio_bridge.qi, audio_bridge._QI_APPLICATION, audio_bridge._QI_APPLICATION_UNAVAILABLE)
        try:
            audio_bridge.qi = FakeQi
            audio_bridge._QI_APPLICATION = None
            audio_bridge._QI_APPLICATION_UNAVAILABLE = False
            first = audio_bridge._connect_broker("tcp://127.0.0.1:9559")
            second = audio_bridge._connect_broker("tcp://127.0.0.1:9559")
            self.assertIs(first, second)
            self.assertEqual(calls["n"], 1)
            self.assertTrue(first.isConnected())
        finally:
            audio_bridge.qi, audio_bridge._QI_APPLICATION, audio_bridge._QI_APPLICATION_UNAVAILABLE = old

    def test_existing_qi_application_connects_a_session(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.url = None

            def connect(self, url: str) -> None:
                self.url = url

        class FakeQi:
            @staticmethod
            def Application(args):
                raise RuntimeError("Application was already initialized")

            @staticmethod
            def Session():
                return Session()

        old = (audio_bridge.qi, audio_bridge._QI_APPLICATION, audio_bridge._QI_APPLICATION_UNAVAILABLE)
        try:
            audio_bridge.qi = FakeQi
            audio_bridge._QI_APPLICATION = None
            audio_bridge._QI_APPLICATION_UNAVAILABLE = False
            session = audio_bridge._connect_broker("tcp://127.0.0.1:9559")
            self.assertEqual(session.url, "tcp://127.0.0.1:9559")
            self.assertTrue(audio_bridge._QI_APPLICATION_UNAVAILABLE)
        finally:
            audio_bridge.qi, audio_bridge._QI_APPLICATION, audio_bridge._QI_APPLICATION_UNAVAILABLE = old

    def test_backoff_resets_after_a_healthy_minute(self) -> None:
        self.assertEqual(audio_bridge.next_backoff(2, 0), 4)
        self.assertEqual(audio_bridge.next_backoff(8, 1), 15)
        self.assertEqual(audio_bridge.next_backoff(15, 61), 2)

    def test_callback_stale_only_while_subscribed(self) -> None:
        self.assertFalse(
            audio_bridge.audio_callbacks_are_stale(False, 0, 10, 2)
        )
        self.assertFalse(
            audio_bridge.audio_callbacks_are_stale(True, 9, 10, 2)
        )
        self.assertTrue(
            audio_bridge.audio_callbacks_are_stale(True, 7, 10, 2)
        )

    def test_stale_callbacks_resubscribe_without_rebuilding_qi(self) -> None:
        self.assertEqual(
            audio_bridge.microphone_command(True, True, True, False),
            "resubscribe",
        )
        self.assertEqual(
            audio_bridge.microphone_command(True, False, False, False),
            "start",
        )
        self.assertIsNone(
            audio_bridge.microphone_command(True, True, False, False)
        )
        self.assertIsNone(
            audio_bridge.microphone_command(True, True, True, True)
        )
        self.assertFalse(audio_bridge.session_should_rebuild(True, 1, 3))
        self.assertTrue(audio_bridge.session_should_rebuild(False, 0, 3))
        self.assertTrue(audio_bridge.session_should_rebuild(True, 3, 3))

    def test_process_remote_counts_drops_without_logging(self) -> None:
        source = audio_bridge.NaoAudioSource.__new__(audio_bridge.NaoAudioSource)
        source.audio_queue = Queue(maxsize=1)
        source.audio_queue.put_nowait(b"old")
        source.dropped_chunks = 0
        source._logged_drops = 0
        source.last_callback_at = None
        warnings: list[tuple] = []

        with patch.object(
            audio_bridge.logger,
            "warning",
            side_effect=lambda *args, **kwargs: warnings.append(args),
        ):
            source.processRemote(1, 1, 0, b"new")
            self.assertEqual(warnings, [])
            audio_bridge.log_dropped_chunks(source)

        self.assertEqual(source.dropped_chunks, 1)
        self.assertIsNotNone(source.last_callback_at)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Dropped audio chunks", warnings[0][0])

    def test_dead_qi_link_does_not_retry_the_microphone(self) -> None:
        """One 'Socket is not connected' must not queue another start."""

        class Source(object):
            def __init__(self) -> None:
                self.subscribed = False
                self.last_callback_at = None
                self.starts = 0

            def stop(self) -> None:
                return None

            def start(self) -> None:
                self.starts += 1
                raise RuntimeError("Socket is not connected")

        source = Source()
        worker = audio_bridge.AudioDeviceWorker(source)
        worker.start()
        try:
            self.assertTrue(worker.submit("start"))
            deadline = time.time() + 2
            while worker.pending is not None and not worker.pending.done:
                if time.time() > deadline:
                    self.fail("start did not finish")
                time.sleep(0.01)
            audio_bridge.maintain_microphone(worker, True, None, time.time())
            self.assertTrue(worker.link_dead)
            self.assertEqual(source.starts, 1)
            self.assertTrue(worker.pending is None or worker.pending.done)
        finally:
            worker.stop_thread(1)

    def test_resubscribe_runs_off_the_caller_thread(self) -> None:
        class Source(object):
            def __init__(self) -> None:
                self.subscribed = True
                self.last_callback_at = 0
                self.actions: list[str] = []
                self.threads: list[int] = []

            def stop(self) -> None:
                self.actions.append("stop")
                self.subscribed = False

            def start(self) -> None:
                self.actions.append("start")
                self.threads.append(threading.current_thread().ident)
                self.subscribed = True
                self.last_callback_at = time.time()

        source = Source()
        worker = audio_bridge.AudioDeviceWorker(source)
        worker.start()
        try:
            self.assertTrue(worker.submit("resubscribe"))
            deadline = time.time() + 2
            while worker.pending is not None and not worker.pending.done:
                if time.time() > deadline:
                    self.fail("resubscribe did not finish")
                time.sleep(0.01)
            worker.poll()
            self.assertEqual(source.actions, ["stop", "start"])
            self.assertNotEqual(source.threads[0], threading.current_thread().ident)
            self.assertEqual(worker.failures, 0)
        finally:
            worker.stop_thread(1)

    def test_stuck_command_and_stalled_loop_request_exit(self) -> None:
        pending = audio_bridge._AudioCommand("start", 0)
        self.assertTrue(
            audio_bridge.command_is_stuck(pending, 10, 10, False)
        )
        self.assertFalse(
            audio_bridge.command_is_stuck(pending, 10, 10, True)
        )
        self.assertTrue(
            audio_bridge.select_loop_is_stalled(True, False, 0, 15, 15)
        )
        self.assertFalse(
            audio_bridge.select_loop_is_stalled(True, True, 0, 15, 15)
        )
        self.assertEqual(audio_bridge.restart_failure_count(2, 1, 60), 3)
        self.assertEqual(audio_bridge.restart_failure_count(2, 61, 60), 0)

    def test_stalled_select_loop_exits_the_process(self) -> None:
        audio_bridge._STOP = False
        audio_bridge._WATCHDOG_STOP = False
        audio_bridge._SELECT_LOOP_ACTIVE = True
        audio_bridge._LOOP_TICK = 0
        exits: list[int] = []

        def fake_exit(code: int) -> None:
            exits.append(code)
            audio_bridge._WATCHDOG_STOP = True

        try:
            with (
                patch.object(audio_bridge.os, "_exit", fake_exit),
                patch.object(audio_bridge.time, "sleep", lambda _seconds: None),
                patch.object(audio_bridge.time, "time", lambda: 100),
            ):
                audio_bridge._watch_select_loop()
        finally:
            audio_bridge._STOP = False
            audio_bridge._WATCHDOG_STOP = True
            audio_bridge._SELECT_LOOP_ACTIVE = False

        self.assertEqual(exits, [1])

    def test_repeated_session_failures_exit_nonzero(self) -> None:
        calls = {"n": 0}

        def boom(_args) -> None:
            calls["n"] += 1
            raise RuntimeError("qi wedged")

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            with (
                patch.object(audio_bridge, "run_session", side_effect=boom),
                patch.object(audio_bridge, "_sleep_interruptible", lambda _seconds: None),
            ):
                code = audio_bridge.main(["--log-file", ""])
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
            audio_bridge._STOP = False
            audio_bridge._WATCHDOG_STOP = True
            audio_bridge._SELECT_LOOP_ACTIVE = False

        self.assertEqual(code, 1)
        self.assertEqual(calls["n"], audio_bridge.SESSION_RESTART_FAIL_LIMIT)

    def test_sigterm_stops_without_a_failure_exit(self) -> None:
        def boom(_args) -> None:
            raise RuntimeError("qi wedged")

        def request_stop(_seconds) -> None:
            audio_bridge._request_stop(signal.SIGTERM, None)

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            with (
                patch.object(audio_bridge, "run_session", side_effect=boom),
                patch.object(audio_bridge, "_sleep_interruptible", request_stop),
            ):
                code = audio_bridge.main(["--log-file", ""])
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
            audio_bridge._STOP = False
            audio_bridge._WATCHDOG_STOP = True
            audio_bridge._SELECT_LOOP_ACTIVE = False

        self.assertEqual(code, 0)

    def test_run_forever_waits_once(self) -> None:
        script = (PACKAGING_DIR / "run_forever.sh").read_text(encoding="utf-8")
        launch_lines = [
            line.strip()
            for line in script.splitlines()
            if "audio_bridge.py" in line and not line.strip().startswith("#")
        ]
        self.assertTrue(launch_lines)
        self.assertTrue(all("--initial-wait" not in line for line in launch_lines))
        self.assertIn("sleep 2", script)


class RunForeverStopTests(unittest.TestCase):
    def test_stop_releases_the_port_when_ps_hides_the_script_name(self) -> None:
        """`ps` shows `python`, not `audio_bridge.py`. Stop must still free the port."""
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        root = Path(home.name)
        port = _free_port()
        (root / "audio_bridge.py").write_text(
            "import os, socket, time\n"
            "sock = socket.socket()\n"
            "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            f"sock.bind(('127.0.0.1', {port}))\n"
            "sock.listen(1)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["NAO_AUDIO_BRIDGE_HOME"] = str(root)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        script = str(PACKAGING_DIR / "run_forever.sh")
        started = subprocess.Popen(
            ["sh", script],
            cwd=str(root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        def _stop_launcher() -> None:
            if started.poll() is None:
                started.kill()
                started.wait(timeout=3)

        self.addCleanup(_stop_launcher)
        self.assertTrue(_port_is_open(port, timeout=8), "bridge did not bind")
        stopped = subprocess.run(
            ["sh", script, "stop"],
            cwd=str(root),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertFalse(_port_is_open(port, timeout=0.4))
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()
        started.wait(timeout=3)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _port_is_open(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        probe = socket.socket()
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    return False


class AutoloadRegistrationTests(unittest.TestCase):
    def test_appends_a_program_section_without_dropping_existing_lines(self) -> None:
        launch = "/home/nao/nao-audio-bridge/run_forever.sh"
        updated = register_autoload.register_program(
            ["[user]", "/home/nao/libfoo.so", ""],
            launch,
        )

        self.assertEqual(
            updated,
            ["[user]", "/home/nao/libfoo.so", "", "[program]", launch],
        )

    def test_inserts_into_an_existing_program_section_once(self) -> None:
        launch = "/home/nao/nao-audio-bridge/run_forever.sh"
        updated = register_autoload.register_program(
            [
                "[program]",
                "/other/bin",
                launch,
                "[python]",
                "/home/nao/other.py",
            ],
            launch,
        )

        self.assertEqual(updated.count(launch), 1)
        self.assertEqual(
            updated,
            [
                "[program]",
                "/other/bin",
                launch,
                "[python]",
                "/home/nao/other.py",
            ],
        )


class _WouldBlock(socket.error):
    def __init__(self) -> None:
        socket.error.__init__(self, errno.EAGAIN, "would block")
        self.errno = None


class WebSocketSocketTests(unittest.TestCase):
    def test_socket_error_code_reads_args_when_errno_is_missing(self) -> None:
        self.assertEqual(socket_error_code(_WouldBlock()), errno.EAGAIN)
        self.assertTrue(is_would_block(_WouldBlock()))
        self.assertIsNone(socket_error_code(socket.error("not a code")))

    def test_bytearray_is_sent_as_bytes(self) -> None:
        seen: list[bytes] = []

        class Sock:
            def send(self, data: bytes) -> int:
                seen.append(data)
                return len(data)

        ws = WebSocket(None, Sock(), ("127.0.0.1", 1))
        remaining = ws._sendBuffer(bytearray(b"abcd"))

        self.assertIsNone(remaining)
        self.assertEqual(seen, [b"abcd"])
        self.assertIsInstance(seen[0], bytes)
        self.assertFalse(ws.send_partial)
        self.assertEqual(ws.bytes_sent, 4)
        self.assertIsNone(ws.last_send_errno)
        self.assertIsNotNone(ws.last_send_progress_at)
        self.assertEqual(as_socket_buffer(bytearray(b"abcd")), b"abcd")

    def test_partial_send_retries_without_dropping_the_frame(self) -> None:
        class Sock:
            def __init__(self) -> None:
                self.calls = 0

            def send(self, data: bytes) -> int:
                self.calls += 1
                if self.calls == 1:
                    return 1
                raise _WouldBlock()

        ws = WebSocket(None, Sock(), ("127.0.0.1", 1))
        remaining = ws._sendBuffer(bytearray(b"abcd"))

        self.assertEqual(remaining, b"bcd")
        self.assertTrue(ws.send_partial)
        self.assertEqual(ws.bytes_sent, 1)
        self.assertEqual(ws.last_send_errno, errno.EAGAIN)

    def test_broken_pipe_still_raises(self) -> None:
        class Sock:
            def send(self, data: bytes) -> int:
                raise socket.error(errno.EPIPE, "broken pipe")

        ws = WebSocket(None, Sock(), ("127.0.0.1", 1))
        with self.assertRaises(socket.error):
            ws._sendBuffer(b"abcd")
        self.assertEqual(ws.last_send_errno, errno.EPIPE)

    def test_eagain_on_recv_waits_instead_of_closing(self) -> None:
        class Sock:
            def recv(self, _n: int) -> bytes:
                raise _WouldBlock()

        ws = WebSocket(None, Sock(), ("127.0.0.1", 1))
        ws.handshaked = True
        ws._handleData()

    def test_prepare_client_socket_disables_nagle_and_enlarges_the_buffer(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            prepare_client_socket(sock)
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)
            self.assertGreaterEqual(
                sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF),
                256 * 1024,
            )
            self.assertFalse(sock.getblocking())
        finally:
            sock.close()


class NaoV4SslCompatTests(unittest.TestCase):
    def test_plain_websocket_imports_without_tlsv1_2(self) -> None:
        script = r"""
import errno
import socket
import sys
import types

ssl = types.ModuleType("ssl")
ssl.PROTOCOL_TLSv1 = 3
sys.modules["ssl"] = ssl

from SimpleWebSocketServer.SimpleWebSocketServer import WebSocket, _SSL_PROTOCOL, _SSL_WANT_ERRORS

assert _SSL_PROTOCOL == 3, _SSL_PROTOCOL
assert _SSL_WANT_ERRORS == ()

class Sock(object):
    def recv(self, n):
        err = socket.error(errno.EAGAIN, "would block")
        if not getattr(err, "errno", None):
            err.errno = errno.EAGAIN
        raise err

ws = WebSocket(None, Sock(), ("127.0.0.1", 1))
ws.handshaked = True
ws._handleData()
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=BRIDGE_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr or completed.stdout,
        )
