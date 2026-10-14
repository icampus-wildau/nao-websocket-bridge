#!/usr/bin/env python2
"""NAO microphone to WebSocket bridge.

Designed to run directly on a NAO with its Python 2.7/NAOqi environment.

The WebSocket protocol is intentionally small:
  * First frame: JSON text describing the audio format.
  * Following frames: raw PCM S16LE mono audio as binary frames.
  * Every few seconds: a JSON status text frame.

Example:
    python audio_bridge.py --port 8765
    python audio_bridge.py --port 8765 --initial-wait 5
    python audio_bridge.py --token secret
"""

from __future__ import print_function

import argparse
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

try:
    import Queue
except ImportError:  # Allows local smoke tests with Python 3 as well.
    import queue as Queue

_QI_IMPORT_ERROR = None
try:
    import qi
except ImportError as exc:  # Helpers are imported in laptop tests without NAOqi.
    qi = None
    _QI_IMPORT_ERROR = exc

from SimpleWebSocketServer import SimpleWebSocketServer, WebSocket


def _load_protocol():
    """Import the shared contract from the install dir or the sibling checkout."""
    try:
        import nao_bridge_protocol
        return nao_bridge_protocol
    except ImportError:
        here = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(os.path.dirname(here), "nao_bridge_protocol")
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
        import nao_bridge_protocol
        return nao_bridge_protocol


_protocol = _load_protocol()
SAMPLE_RATE = _protocol.SAMPLE_RATE
CHANNELS = _protocol.CHANNELS
SAMPLE_WIDTH_BYTES = _protocol.SAMPLE_WIDTH_BYTES
FRONT_MIC = _protocol.FRONT_MIC
path_is_authorized = _protocol.path_is_authorized
status_message = _protocol.status_message
audio_format_message = _protocol.audio_format_message

SERVICE_NAME = "NaoAudioBridge"
QUEUE_SIZE = 16
MAX_RETRIES = 5
RETRY_DELAY = 2.0
# ALAudioDevice delivers one buffer about every 170 ms. Three frames is
# about half a second: long enough to ride out a Wi-Fi pause, short enough
# that the laptop hears recent audio instead of a growing delay.
SENDQ_MAX_FRAMES = 3
# Close only when the kernel has accepted nothing for this long while frames
# are still waiting. A long queue by itself is a stall, not a dead socket.
SEND_STALL_SECS = 5.0
# WebSocket binary opcode. Only whole PCM frames are discarded; a frame
# already partly written stays at the head of the queue.
_WS_BINARY = 0x2
BACKOFF_START_SECS = 2.0
BACKOFF_MAX_SECS = 15.0
HEALTHY_RESET_SECS = 60.0
UNSUBSCRIBE_GRACE_SECS = 2.0
CALLBACK_STALE_SECS = 2.0
STATUS_INTERVAL_SECS = 5.0
DROP_LOG_INTERVAL_SECS = 1.0
# A subscribe/unsubscribe that does not return is a wedged qi runtime.
QI_CALL_TIMEOUT_SECS = 10.0
# The WebSocket thread updates a tick every pass. A stuck tick means a
# blocking call is still on this thread, so the process must be replaced.
SELECT_STALL_SECS = 15.0
RESUBSCRIBE_FAIL_LIMIT = 3
SESSION_RESTART_FAIL_LIMIT = 3
DEFAULT_LOG_FILE = "/home/nao/nao-audio-bridge/audio_bridge.log"

logger = logging.getLogger("nao_audio_bridge")

# Set for the lifetime of one serve session. The WebSocket class is
# constructed by SimpleWebSocketServer and cannot take extra arguments.
BRIDGE = None
_STOP = False
_SELECT_LOOP_ACTIVE = False
_LOOP_TICK = 0.0
_WATCHDOG_STOP = False


def _mapping_field(service, key):
    """Read a field from a dict or a qi map, which has no ``get`` method."""
    try:
        return service[key]
    except Exception:
        return None


def stale_service_ids(services, name):
    """Return ServiceDirectory ids whose service name matches ``name``."""
    ids = []
    for service in services:
        if _mapping_field(service, "name") != name:
            continue
        service_id = _mapping_field(service, "serviceId")
        if service_id is None:
            service_id = _mapping_field(service, "id")
        if service_id is not None:
            ids.append(service_id)
    return ids


def trim_sendq(client, max_frames=SENDQ_MAX_FRAMES):
    """Drop oldest whole PCM frames until ``sendq`` is within ``max_frames``.

    The head frame is kept when a send is already in progress, because those
    bytes are on the wire. Text and close frames are kept. Returns how many
    PCM frames were discarded.
    """
    sendq = getattr(client, "sendq", None)
    if sendq is None or len(sendq) <= max_frames:
        return 0

    items = list(sendq)
    protect_head = bool(getattr(client, "send_partial", False))
    excess = len(items) - max_frames
    kept = []
    dropped = 0
    for index, item in enumerate(items):
        opcode = item[0]
        if (
            excess > 0
            and opcode == _WS_BINARY
            and not (protect_head and index == 0)
        ):
            excess -= 1
            dropped += 1
            continue
        kept.append(item)

    if not dropped:
        return 0

    while True:
        try:
            sendq.popleft()
        except IndexError:
            break
    for item in kept:
        sendq.append(item)
    return dropped


def queue_client_message(client, data, max_frames=SENDQ_MAX_FRAMES):
    """Queue one WebSocket frame, discarding stale PCM if the queue is long."""
    client.sendMessage(data)
    return trim_sendq(client, max_frames)


def send_is_stalled(client, now, stall_after=SEND_STALL_SECS):
    """True when queued frames have made no send progress for ``stall_after``."""
    sendq = getattr(client, "sendq", None)
    if not sendq:
        return False
    last = getattr(client, "last_send_progress_at", None)
    if last is None:
        last = getattr(client, "connected_at", None)
    if last is None:
        return False
    return (now - last) >= stall_after


def enqueue_pcm(audio_queue, pcm):
    """Enqueue ``pcm``, dropping the oldest chunk when the queue is full.

    Returns how many chunks were discarded (0, 1, or 2).
    """
    try:
        audio_queue.put_nowait(pcm)
        return 0
    except Queue.Full:
        dropped = 1

    try:
        audio_queue.get_nowait()
    except Queue.Empty:
        pass

    try:
        audio_queue.put_nowait(pcm)
    except Queue.Full:
        dropped += 1
    return dropped


def session_is_connected(session):
    """False when qi exposes ``isConnected`` and the session is down."""
    if session is None:
        return False
    is_connected = getattr(session, "isConnected", None)
    if is_connected is None:
        return True
    try:
        return bool(is_connected())
    except Exception:
        return False


def audio_callbacks_are_stale(subscribed, last_callback_at, now, stale_after):
    """True when a subscribed mic has not delivered audio recently."""
    if not subscribed or last_callback_at is None:
        return False
    return (now - last_callback_at) >= stale_after


def microphone_command(has_clients, subscribed, stale, command_inflight):
    """What to ask ALAudioDevice for, without touching qi.

    A stale mic with a client still connected is resubscribed in place.
    ``None`` leaves the current subscription alone (including the timed
    unsubscribe, which is decided by the caller).
    """
    if command_inflight or not has_clients:
        return None
    if not subscribed:
        return "start"
    if stale:
        return "resubscribe"
    return None


def qi_link_is_dead(exc):
    """True when a qi call failed because that session's socket is gone.

    ``ALAudioDevice`` raises ``Socket is not connected`` while
    ``session.isConnected()`` can still be true. Retrying the microphone
    then burns the resubscribe limit in a few tens of milliseconds.
    """
    return "not connected" in str(exc).lower()


def session_should_rebuild(session_connected, resubscribe_failures, limit):
    """Rebuild qi only when the session is down or resubscribe keeps failing."""
    if not session_connected:
        return True
    return resubscribe_failures >= limit


def command_is_stuck(pending, now, timeout, stop):
    """True when a qi audio call has not finished and stop was not requested."""
    if stop or pending is None or pending.done:
        return False
    return (now - pending.started_at) >= timeout


def select_loop_is_stalled(active, stop, last_tick, now, stall_after):
    """True when the WebSocket thread has stopped updating its tick."""
    if stop or not active:
        return False
    return (now - last_tick) >= stall_after


def restart_failure_count(previous, elapsed, healthy_after):
    """Count short session restarts. A healthy stretch clears the run."""
    if elapsed >= healthy_after:
        return 0
    return previous + 1


def next_backoff(delay, elapsed, healthy_after=HEALTHY_RESET_SECS,
                 start=BACKOFF_START_SECS, maximum=BACKOFF_MAX_SECS):
    """Double the restart delay, or reset it after a healthy stretch."""
    if elapsed >= healthy_after:
        return start
    doubled = delay * 2
    if doubled > maximum:
        return maximum
    return doubled


def configure_logging(log_path):
    """Log to stderr and, when possible, a 1 MB rotating file."""
    logger.setLevel(logging.INFO)
    logger.handlers = []
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    logger.propagate = False

    if not log_path:
        return

    try:
        directory = os.path.dirname(log_path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=1024 * 1024,
            backupCount=1,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as exc:
        logger.warning("Could not open log file %s: %s", log_path, exc)


def retry_operation(description, operation, max_retries=MAX_RETRIES, retry_delay=RETRY_DELAY):
    """Run operation with retries; raise the last error when all attempts fail."""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            logger.warning(
                "Failed to %s (attempt %d/%d): %s. Retrying in %.1fs...",
                description,
                attempt,
                max_retries,
                exc,
                retry_delay,
            )
            _sleep_interruptible(retry_delay)
            if _STOP:
                raise last_error

    logger.error(
        "Failed to %s after %d attempts: %s",
        description,
        max_retries,
        last_error,
    )
    raise last_error


def _sleep_interruptible(seconds):
    deadline = time.time() + seconds
    while not _STOP:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def _client_host(client):
    address = getattr(client, "address", None)
    if not address:
        return ""
    return address[0]


class BridgeState(object):
    """Clients and mic-subscribe requests for the active serve loop."""

    def __init__(self, token):
        self.token = token or ""
        self.clients = []
        self.send_discarded = 0
        self._last_send_discard_log_at = 0.0
        self._logged_send_discards = 0
        self._last_reject_log_at = 0.0

    def note_send_discards(self, client, dropped):
        if dropped <= 0:
            return
        self.send_discarded += dropped
        now = time.time()
        if now - self._last_send_discard_log_at < DROP_LOG_INTERVAL_SECS:
            return
        sendq = getattr(client, "sendq", None)
        depth = len(sendq) if sendq is not None else 0
        logger.warning(
            "Discarded queued audio frames: %d (+%d), send queue depth %d",
            self.send_discarded,
            self.send_discarded - self._logged_send_discards,
            depth,
        )
        self._logged_send_discards = self.send_discarded
        self._last_send_discard_log_at = now

    def authorize(self, client):
        request = getattr(client, "request", None)
        path = ""
        if request is not None:
            path = getattr(request, "path", "") or ""
        return path_is_authorized(path, self.token)

    def add_client(self, client):
        """Keep one client.

        A new handshake from the same machine replaces the previous socket, so
        a restarted PC app can take over. A different machine must not: both
        sides otherwise reconnect the moment they are closed and swap the
        microphone dozens of times a second.
        """
        for other in list(self.clients):
            if other is client:
                continue
            if _client_host(other) == _client_host(client):
                self.drop_client(other, "replaced by a new client")
                continue
            self._reject_extra_client(client, other)
            return False
        if client not in self.clients:
            self.clients.append(client)
        return True

    def _reject_extra_client(self, client, keeper):
        now = time.time()
        if now - self._last_reject_log_at >= 1.0:
            client_address = getattr(client, "address", ("?", "?"))
            keeper_address = getattr(keeper, "address", ("?", "?"))
            logger.warning(
                "Rejected WebSocket client %s:%s: %s:%s is already connected",
                client_address[0],
                client_address[1],
                keeper_address[0],
                keeper_address[1],
            )
            self._last_reject_log_at = now
        try:
            client.close()
        except Exception as exc:
            logger.warning("Error closing WebSocket client: %s", exc)

    def remove_client(self, client):
        try:
            self.clients.remove(client)
        except ValueError:
            return False
        return True

    def drop_client(self, client, reason):
        removed = self.remove_client(client)
        address = getattr(client, "address", ("?", "?"))
        logger.warning(
            "Closing WebSocket client %s:%s: %s",
            address[0],
            address[1],
            reason,
        )
        try:
            client.close()
        except Exception as exc:
            logger.warning("Error closing WebSocket client: %s", exc)
        return removed


class AudioWebSocket(WebSocket):
    """WebSocket connection receiving the NAO microphone stream."""

    def handleConnected(self):
        state = BRIDGE
        if state is None:
            return
        if not state.authorize(self):
            logger.warning(
                "Rejected WebSocket client %s:%s: bad token path",
                self.address[0],
                self.address[1],
            )
            try:
                self.close()
            except Exception as exc:
                logger.warning("Error closing rejected client: %s", exc)
            return

        if not state.add_client(self):
            return
        self.sendMessage(audio_format_message())
        logger.info("WebSocket client connected: %s:%s", self.address[0], self.address[1])

    def handleMessage(self):
        # The bridge is NAO -> client, plus optional status text.
        pass

    def handleClose(self):
        state = BRIDGE
        if state is not None and state.remove_client(self):
            logger.info(
                "WebSocket client disconnected: %s:%s",
                self.address[0],
                self.address[1],
            )


class BridgeSource(object):
    """One realtime stream the bridge can start while a client is connected.

    ``start`` subscribes a NAOqi device and ``stop`` releases it. The device
    callback only enqueues bytes. Text envelopes for the stream come from
    ``nao_bridge_protocol``. The front microphone is the first source.
    """

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError


class NaoAudioSource(BridgeSource):
    """Receives microphone PCM buffers from ALAudioDevice."""

    def __init__(self, session, audio_queue):
        self.audio_device = session.service("ALAudioDevice")
        self.audio_queue = audio_queue
        self.subscribed = False
        self.dropped_chunks = 0
        self.last_callback_at = None
        self._logged_drops = 0

    def start(self):
        try:
            try:
                self.audio_device.unsubscribe(SERVICE_NAME)
            except Exception:
                # A fresh NAOqi has nothing registered under this name.
                pass
            self.audio_device.setClientPreferences(
                SERVICE_NAME,
                SAMPLE_RATE,
                FRONT_MIC,
                0,
            )
            self.audio_device.subscribe(SERVICE_NAME)
        except Exception:
            self.subscribed = False
            raise

        self.subscribed = True
        self.last_callback_at = time.time()
        logger.info("Subscribed to NAO front microphone at %d Hz.", SAMPLE_RATE)

    def stop(self):
        if not self.subscribed:
            return

        try:
            self.audio_device.unsubscribe(SERVICE_NAME)
        except Exception as exc:
            logger.warning("Error unsubscribing from NAO microphone: %s", exc)
        finally:
            self.subscribed = False
            self.last_callback_at = None
            logger.info("Unsubscribed from NAO microphone.")

    def processRemote(
        self,
        nbOfChannels,
        nbOfSamplesByChannel,
        timestamp,
        inputBuffer,
    ):
        """NAOqi callback; keep this path short and non-blocking."""
        self.last_callback_at = time.time()
        if sys.version_info[0] >= 3:
            pcm = bytes(inputBuffer)
        else:
            # NAOqi's Python 2 binding exposes the raw qi buffer so that
            # str(buffer) yields the PCM bytes.
            pcm = str(inputBuffer)
        if not pcm:
            return

        # The NAOqi callback must only count. Logging happens on the
        # WebSocket thread, where it cannot stall audio delivery.
        dropped = enqueue_pcm(self.audio_queue, pcm)
        if dropped:
            self.dropped_chunks += dropped


def unregister_stale_services(session, name):
    """Drop a leftover qi service so registerService can claim ``name``."""
    directory = session.service("ServiceDirectory")
    services = directory.services()
    for service_id in stale_service_ids(services, name):
        try:
            session.unregisterService(service_id)
            logger.info("Unregistered stale service %s id %s", name, service_id)
        except Exception as exc:
            logger.warning(
                "Could not unregister stale service %s id %s: %s",
                name,
                service_id,
                exc,
            )


def broadcast_pending_audio(state, audio_queue):
    """Move queued PCM into the single client's WebSocket send queue."""
    while True:
        try:
            pcm = audio_queue.get_nowait()
        except Queue.Empty:
            return

        if not state.clients:
            continue

        frame = bytearray(pcm)
        for client in list(state.clients):
            try:
                dropped = queue_client_message(client, frame)
            except Exception as exc:
                state.drop_client(client, exc)
                continue
            state.note_send_discards(client, dropped)


def send_status(state, audio_source):
    if not state.clients or audio_source is None:
        return
    payload = status_message(
        audio_source.dropped_chunks,
        audio_source.subscribed,
    )
    for client in list(state.clients):
        try:
            dropped = queue_client_message(client, payload)
        except Exception as exc:
            state.drop_client(client, exc)
            continue
        state.note_send_discards(client, dropped)


def drop_stalled_clients(state, now):
    """Drop a client only when the socket has accepted no bytes for a while."""
    for client in list(state.clients):
        if send_is_stalled(client, now):
            state.drop_client(client, "send stalled")


def log_send_progress(state):
    """One line per client: queue depth, bytes the kernel accepted, last errno."""
    for client in list(state.clients):
        sendq = getattr(client, "sendq", None)
        depth = len(sendq) if sendq is not None else 0
        address = getattr(client, "address", ("?", "?"))
        logger.info(
            "WebSocket %s:%s send queued=%d bytes_sent=%d last_errno=%s",
            address[0],
            address[1],
            depth,
            int(getattr(client, "bytes_sent", 0) or 0),
            getattr(client, "last_send_errno", None),
        )


class _AudioCommand(object):
    def __init__(self, action, started_at):
        self.action = action
        self.started_at = started_at
        self.error = None
        self.done = False


class AudioDeviceWorker(object):
    """Subscribe and unsubscribe on a helper thread, not the socket thread."""

    def __init__(self, audio_source):
        self.audio_source = audio_source
        self.failures = 0
        self.link_dead = False
        self.pending = None
        self._queue = Queue.Queue()
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True

    def start(self):
        self._thread.start()

    def submit(self, action):
        if self.pending is not None and not self.pending.done:
            return False
        command = _AudioCommand(action, time.time())
        self.pending = command
        self._queue.put(command)
        return True

    def poll(self):
        """Take a finished command. Failures count toward a qi rebuild."""
        command = self.pending
        if command is None or not command.done:
            return None
        self.pending = None
        if command.error is not None:
            self.failures += 1
            if qi_link_is_dead(command.error):
                self.link_dead = True
        elif command.action != "stop":
            self.failures = 0
        return command

    def stop_thread(self, timeout):
        """Ask the helper to exit and wait. True when it did."""
        self._queue.put(None)
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self):
        while True:
            try:
                command = self._queue.get(timeout=0.2)
            except Queue.Empty:
                if _STOP:
                    return
                continue
            if command is None:
                return
            try:
                if command.action == "stop":
                    self.audio_source.stop()
                else:
                    self.audio_source.stop()
                    self.audio_source.start()
            except Exception as exc:
                command.error = exc
            command.done = True


def log_dropped_chunks(audio_source):
    """Log mic drops from the socket thread. The callback only counts them."""
    if audio_source is None:
        return
    pending = audio_source.dropped_chunks - audio_source._logged_drops
    if pending <= 0:
        return
    logger.warning(
        "Dropped audio chunks: %d (+%d)",
        audio_source.dropped_chunks,
        pending,
    )
    audio_source._logged_drops = audio_source.dropped_chunks


def maintain_microphone(worker, has_clients, empty_since, now):
    """Queue subscribe/unsubscribe. Never call ALAudioDevice on this thread.

    Returns the updated ``empty_since`` timestamp.
    """
    finished = worker.poll()
    if finished is not None and finished.error is not None:
        logger.error(
            "Microphone %s failed: %s",
            finished.action,
            finished.error,
        )
    if worker.link_dead:
        return empty_since

    inflight = worker.pending is not None and not worker.pending.done
    source = worker.audio_source
    stale = audio_callbacks_are_stale(
        source.subscribed,
        source.last_callback_at,
        now,
        CALLBACK_STALE_SECS,
    )
    action = microphone_command(
        has_clients,
        source.subscribed,
        stale,
        inflight,
    )
    if has_clients:
        empty_since = None
        if action == "resubscribe":
            logger.warning("Microphone callbacks are stale; resubscribing in place")
        if action is not None:
            worker.submit(action)
        return empty_since

    if empty_since is None:
        empty_since = now
    if (
        not inflight
        and source.subscribed
        and (now - empty_since) >= UNSUBSCRIBE_GRACE_SECS
    ):
        worker.submit("stop")
    return empty_since


def _watch_select_loop():
    """Exit the process if the WebSocket thread stops making progress."""
    while not _WATCHDOG_STOP and not _STOP:
        time.sleep(0.2)
        if _WATCHDOG_STOP or _STOP:
            return
        if select_loop_is_stalled(
            _SELECT_LOOP_ACTIVE,
            _STOP,
            _LOOP_TICK,
            time.time(),
            SELECT_STALL_SECS,
        ):
            logger.error(
                "WebSocket select loop stalled for %.1fs; exiting",
                SELECT_STALL_SECS,
            )
            os._exit(1)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="NAO microphone WebSocket bridge")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="WebSocket bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="WebSocket port (default: 8765)",
    )
    parser.add_argument(
        "--qi-url",
        default="tcp://127.0.0.1:9559",
        help="Local NAOqi URL (default: tcp://127.0.0.1:9559)",
    )
    parser.add_argument(
        "--initial-wait",
        type=float,
        default=0.0,
        help="Seconds to wait before the first startup (default: 0)",
    )
    parser.add_argument(
        "--token",
        default="",
        help="When set, only ws://host:port/<token> is accepted",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        help="Rotating log path (default: %s)" % DEFAULT_LOG_FILE,
    )
    return parser.parse_args(argv)


def _request_stop(signum, frame):
    global _STOP
    _STOP = True


# qi.Application is a process-wide singleton. Constructing it again raises
# "Application was already initialized", including on the supervisor's next
# attempt in this same interpreter.
_QI_APPLICATION = None
_QI_APPLICATION_UNAVAILABLE = False


def _qi_unavailable_error():
    return RuntimeError(
        "qi is not available (%s). interpreter=%s version=%s"
        % (_QI_IMPORT_ERROR, sys.executable, sys.version.split()[0])
    )


def _already_initialized(exc):
    return "already initialized" in str(exc).lower() or "already started" in str(exc).lower()


def _connect_broker(qi_url):
    """Return a session on the running NAOqi broker.

    Retries reuse the application created by the first attempt. When this
    interpreter already has one (NAOqi embedded the script, or a previous
    attempt created it), connect a Session instead of constructing another.
    """
    global _QI_APPLICATION, _QI_APPLICATION_UNAVAILABLE

    if qi is None:
        raise _qi_unavailable_error()

    if _QI_APPLICATION is None and not _QI_APPLICATION_UNAVAILABLE:
        try:
            _QI_APPLICATION = qi.Application([
                "NaoAudioBridge",
                "--qi-url=" + qi_url,
            ])
        except Exception as exc:
            if not _already_initialized(exc):
                raise
            logger.info("Reusing the qi application already running in this process")
            _QI_APPLICATION_UNAVAILABLE = True

    if _QI_APPLICATION is not None:
        start = getattr(_QI_APPLICATION, "start", None)
        if start is not None:
            try:
                start()
            except Exception as exc:
                if not _already_initialized(exc):
                    raise
        session = _QI_APPLICATION.session
        if not session_is_connected(session):
            session.connect(qi_url)
        return session

    session = qi.Session()
    session.connect(qi_url)
    return session


def _connect_qi(qi_url):
    if qi is None:
        raise _qi_unavailable_error()
    return retry_operation("connect to NAOqi", lambda: _connect_broker(qi_url))


def run_session(args):
    """Serve until asked to stop, or until qi itself must be rebuilt."""
    global BRIDGE, _SELECT_LOOP_ACTIVE, _LOOP_TICK

    session = None
    service_id = None
    server = None
    audio_source = None
    audio_worker = None
    state = BridgeState(args.token)
    BRIDGE = state
    empty_since = None
    last_status = time.time()

    def cleanup():
        global BRIDGE
        if audio_worker is not None:
            if audio_source is not None and audio_source.subscribed:
                audio_worker.submit("stop")
            finished = audio_worker.stop_thread(UNSUBSCRIBE_GRACE_SECS)
            if not finished and not _STOP:
                logger.error("NAOqi audio call is stuck; exiting")
                os._exit(1)
        elif audio_source is not None:
            try:
                audio_source.stop()
            except Exception as exc:
                logger.warning("Error stopping audio source: %s", exc)

        if server is not None:
            try:
                server.close()
            except Exception as exc:
                logger.warning("Error closing WebSocket server: %s", exc)

        if service_id is not None and session is not None:
            try:
                session.unregisterService(service_id)
            except Exception as exc:
                logger.warning("Error unregistering service: %s", exc)

        # Leave the process-wide qi.Application running. Stopping it does not
        # clear the singleton, so the next attempt could not construct another.

        if audio_source is not None and audio_source.dropped_chunks:
            logger.info("Dropped audio chunks: %d", audio_source.dropped_chunks)
        BRIDGE = None

    try:
        session = _connect_qi(args.qi_url)

        # ALAudioDevice calls processRemote() back through qi. Since this
        # program runs on the NAO itself, loopback is a valid endpoint.
        # A rebuilt session in this process may already be listening.
        def listen():
            try:
                session.listen("tcp://127.0.0.1:0")
            except Exception as exc:
                if not _already_initialized(exc) and "already" not in str(exc).lower():
                    raise

        retry_operation("register session listener", listen)

        audio_queue = Queue.Queue(maxsize=QUEUE_SIZE)
        audio_source = NaoAudioSource(session, audio_queue)
        audio_worker = AudioDeviceWorker(audio_source)
        audio_worker.start()
        unregister_stale_services(session, SERVICE_NAME)
        service_id = retry_operation(
            "register audio service",
            lambda: session.registerService(SERVICE_NAME, audio_source),
        )

        server = SimpleWebSocketServer(
            args.host,
            args.port,
            AudioWebSocket,
            selectInterval=0.01,
        )
        logger.info(
            "WebSocket audio bridge listening on ws://%s:%d",
            args.host,
            args.port,
        )

        _SELECT_LOOP_ACTIVE = True
        _LOOP_TICK = time.time()
        while not _STOP:
            _LOOP_TICK = time.time()
            try:
                server.serveonce()
            except Exception as exc:
                logger.error("WebSocket loop error: %s", exc)
                raise
            _LOOP_TICK = time.time()

            broadcast_pending_audio(state, audio_queue)
            now = time.time()
            _LOOP_TICK = now
            drop_stalled_clients(state, now)
            empty_since = maintain_microphone(
                audio_worker,
                bool(state.clients),
                empty_since,
                now,
            )
            if command_is_stuck(
                audio_worker.pending,
                now,
                QI_CALL_TIMEOUT_SECS,
                _STOP,
            ):
                logger.error("NAOqi audio call is stuck; exiting")
                os._exit(1)
            if audio_worker.link_dead or not session_is_connected(session):
                logger.error("NAOqi session is down; reconnecting")
                return
            if session_should_rebuild(
                True,
                audio_worker.failures,
                RESUBSCRIBE_FAIL_LIMIT,
            ):
                logger.error(
                    "Microphone resubscribe failed %d times; reconnecting",
                    audio_worker.failures,
                )
                return
            if now - last_status >= STATUS_INTERVAL_SECS:
                send_status(state, audio_source)
                log_send_progress(state)
                log_dropped_chunks(audio_source)
                last_status = now
    finally:
        _SELECT_LOOP_ACTIVE = False
        cleanup()


def main(argv=None):
    global _STOP, _WATCHDOG_STOP, _SELECT_LOOP_ACTIVE
    args = parse_args(argv)
    configure_logging(args.log_file)
    _STOP = False
    _WATCHDOG_STOP = False
    _SELECT_LOOP_ACTIVE = False

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    watchdog = threading.Thread(target=_watch_select_loop)
    watchdog.daemon = True
    watchdog.start()
    try:
        if args.initial_wait > 0:
            logger.info("Waiting %.1fs before startup.", args.initial_wait)
            _sleep_interruptible(args.initial_wait)

        delay = BACKOFF_START_SECS
        failures = 0
        while not _STOP:
            started = time.time()
            try:
                run_session(args)
            except Exception as exc:
                logger.error("Audio bridge session failed: %s", exc)
            elapsed = time.time() - started
            if _STOP:
                break
            failures = restart_failure_count(
                failures,
                elapsed,
                HEALTHY_RESET_SECS,
            )
            if failures >= SESSION_RESTART_FAIL_LIMIT:
                logger.error(
                    "NAOqi session failed %d times in a row; exiting for a clean interpreter",
                    failures,
                )
                return 1
            if elapsed >= HEALTHY_RESET_SECS:
                delay = BACKOFF_START_SECS
            logger.info("Restarting audio bridge in %.1fs", delay)
            _sleep_interruptible(delay)
            if elapsed < HEALTHY_RESET_SECS:
                delay = next_backoff(delay, 0)

        logger.info("Audio bridge stopped.")
        return 0
    finally:
        _WATCHDOG_STOP = True
        _SELECT_LOOP_ACTIVE = False
        watchdog.join(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
