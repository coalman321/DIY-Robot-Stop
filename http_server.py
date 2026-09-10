import usocket as socket
import ujson as json
import uos as os
from time import ticks_ms, ticks_diff
import uerrno as _errno

_EAGAIN = getattr(_errno, 'EAGAIN', 11)
_EWOULDBLOCK = getattr(_errno, 'EWOULDBLOCK', _EAGAIN)

from estop_common import StopState

# Root of the static site on the device filesystem (the `www/` folder that
# ships alongside this file). Holds the RacingScoreWebApp: index.html, css/,
# js/, images/. No 3D models and no external CDN scripts, so it works fully
# offline behind the Pico's access point.
WWW_ROOT = "www"

# File extension -> Content-Type. Anything unlisted is sent as octet-stream.
_CONTENT_TYPES = {
    "html": "text/html",
    "css": "text/css",
    "js": "application/javascript",
    "json": "application/json",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "ico": "image/x-icon",
    "txt": "text/plain",
}
_TEXT_TYPES = ("text/", "application/javascript", "application/json", "image/svg+xml")

_FILE_CHUNK = 512


def _is_eagain(exc):
    a = exc.args[0] if exc.args else None
    return a == _EAGAIN or a == _EWOULDBLOCK


class EStopHTTPServer:
    """A tiny HTTP server for the e-stop.

    Routes:
      * GET /api/events        -> a Server-Sent Events stream that pushes
                                  {"stopped": 0|1, "stopped_ids": [...]} on every
                                  state change and on a ~2 s heartbeat. This is
                                  the connection the web page uses.
      * GET /api/status        -> the same JSON once (polling fallback).
      * GET /favicon.ico       -> 204, so a browser doesn't burn a connection
                                  on a 404 every page load.
      * GET /  and everything else -> a file from WWW_ROOT ("/" -> index.html)

    Why SSE: the page previously polled /api/status several times a second, and
    every response closed its TCP connection server-side. Each close left a
    TIME_WAIT control block in the Pico's small lwIP pool; at a few connections
    per second the pool stayed exhausted and new connections were dropped in
    bursts. One long-lived SSE stream removes that churn.

    The listening socket is non-blocking and `poll_data()` drains everything
    pending each call, so the safety loop stays responsive. Static-file
    transfers still block for the length of the transfer (loaded once per
    session). Finished connections are closed only after the client's FIN
    arrives, so the Pico is the passive closer and accrues no TIME_WAIT.
    """

    def __init__(self, host='0.0.0.0', port=80, www_root=WWW_ROOT, client_timeout=1.0):
        self.host = host
        self.port = port
        self.www_root = www_root
        self.client_timeout = client_timeout
        self._socket = None

        # Live SSE subscribers: [{'sock': socket, 'fails': int}, ...]
        self._sse = []
        self._sse_last_frame = None
        self._sse_last_emit = 0

        # Connections whose response is fully sent, waiting for the peer's FIN
        # so we can close from the passive side: [[sock, started_ms], ...]
        self._closing = []

        # Tunables.
        self._sse_max = 4               # concurrent event streams
        self._sse_max_fails = 8         # consecutive EAGAIN sends before drop
        self._heartbeat_ms = 2000       # re-send state even if unchanged
        self._close_wait_ms = 500       # give up waiting for a peer FIN
        self._max_accept_per_poll = 8

    # ------------------------------------------------------------------ loop
    def poll_data(self, stop_state: StopState):
        sock = self._socket
        if sock is None:
            sock = self._create_socket()
            if sock is None:
                return None

        # 1. Accept every pending connection this cycle so a burst of parallel
        #    requests can't overflow the small listen backlog.
        for _ in range(self._max_accept_per_poll):
            try:
                client_socket, _ = sock.accept()
            except OSError:
                break
            self._on_accept(client_socket, stop_state)

        # 2. Fan the current state out to SSE subscribers.
        self._push_sse(stop_state)

        # 3. Progress any connections waiting to close.
        self._reap_closing()

        return None

    def _create_socket(self):
        try:
            addr_info = socket.getaddrinfo(self.host, self.port)[0][-1]
            server_socket = socket.socket()
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind(addr_info)
            server_socket.listen(4)
            # Non-blocking accept: the safety loop must not wait on the network.
            try:
                server_socket.setblocking(False)
            except AttributeError:
                pass
            self._socket = server_socket
            return server_socket
        except OSError:
            self._socket = None
            return None

    # --------------------------------------------------------------- accept
    def _on_accept(self, client, stop_state):
        try:
            client.settimeout(self.client_timeout)
        except (AttributeError, OSError):
            pass

        try:
            request = client.recv(1024)
        except OSError:
            self._hard_close(client)
            return
        if not request:
            self._hard_close(client)
            return

        try:
            self._route(client, request.decode('utf-8'), stop_state)
        except Exception:
            # A malformed request must not bubble up and reboot the device
            # (main.run_supervised resets on any exception).
            self._hard_close(client)

    # -------------------------------------------------------------- routing
    def _route(self, client, request: str, stop_state: StopState):
        request_line = request.split('\r\n', 1)[0]
        parts = request_line.split(' ')
        path = parts[1] if len(parts) > 1 else '/'
        # Drop query string / fragment.
        path = path.split('?', 1)[0].split('#', 1)[0]

        if path == '/api/events':
            self._start_sse(client, stop_state)
            return

        if path == '/api/status':
            body = self._build_status(stop_state).encode('utf-8')
            self._send_head(client, '200 OK', 'application/json', len(body), cache=False)
            self._send_all(client, body)
            self._finish(client)
            return

        if path == '/favicon.ico':
            self._send_head(client, '204 No Content', 'text/plain', 0, cache=True)
            self._finish(client)
            return

        self._serve_file(client, path)

    def _build_status(self, stop_state: StopState):
        return json.dumps({
            'stopped': 1 if stop_state.stopped else 0,
            'stopped_ids': list(stop_state.stopped_ids),
        })

    # ------------------------------------------------------------ SSE stream
    def _sse_frame(self, stop_state: StopState):
        return ('data: ' + self._build_status(stop_state) + '\n\n').encode('utf-8')

    def _start_sse(self, client, stop_state):
        if len(self._sse) >= self._sse_max:
            self._send_simple(client, '503 Service Unavailable', 'Too many event streams')
            self._finish(client)
            return

        head = (
            'HTTP/1.1 200 OK\r\n'
            'Content-Type: text/event-stream; charset=utf-8\r\n'
            'Cache-Control: no-store\r\n'
            'Connection: keep-alive\r\n'
            'X-Accel-Buffering: no\r\n'
            '\r\n'
            'retry: 3000\n\n'
        ).encode('utf-8')
        try:
            self._send_all(client, head)
        except OSError:
            self._hard_close(client)
            return

        # Non-blocking for the lifetime of the stream: a slow reader must never
        # stall the safety loop.
        try:
            client.setblocking(False)
        except (AttributeError, OSError):
            pass

        try:
            client.send(self._sse_frame(stop_state))
        except OSError:
            pass

        self._sse.append({'sock': client, 'fails': 0})
        # Force the next _push_sse to emit even if the state hasn't changed.
        self._sse_last_frame = None

    def _push_sse(self, stop_state):
        if not self._sse:
            return

        frame = self._sse_frame(stop_state)
        now = ticks_ms()
        changed = frame != self._sse_last_frame
        due = ticks_diff(now, self._sse_last_emit) >= self._heartbeat_ms
        if not (changed or due):
            self._sse_reap()
            return

        survivors = []
        for entry in self._sse:
            sock = entry['sock']
            if self._peer_closed(sock):
                self._hard_close(sock)
                continue
            try:
                n = sock.send(frame)
                if n == len(frame):
                    entry['fails'] = 0
                    survivors.append(entry)
                else:
                    # Partial write on a tiny frame -- the client is backed up.
                    # Drop it; EventSource will reconnect cleanly.
                    self._hard_close(sock)
            except OSError as exc:
                if _is_eagain(exc):
                    entry['fails'] += 1
                    if entry['fails'] <= self._sse_max_fails:
                        survivors.append(entry)
                    else:
                        self._hard_close(sock)
                else:
                    self._hard_close(sock)

        self._sse = survivors
        self._sse_last_frame = frame
        self._sse_last_emit = now

    def _sse_reap(self):
        """Cheap per-loop check for subscribers that have gone away."""
        survivors = []
        for entry in self._sse:
            if self._peer_closed(entry['sock']):
                self._hard_close(entry['sock'])
            else:
                survivors.append(entry)
        self._sse = survivors

    @staticmethod
    def _peer_closed(sock):
        try:
            data = sock.recv(64)
            # b'' => the peer sent FIN. None => would-block (some MicroPython
            # builds return None instead of raising EAGAIN); still open.
            return data == b''
        except OSError as exc:
            return not _is_eagain(exc)  # EAGAIN => still open

    # ----------------------------------------------------------- static files
    def _serve_file(self, client, path: str):
        try:
            self._serve_file_inner(client, path)
        finally:
            self._finish(client)

    def _serve_file_inner(self, client, path: str):
        if path in ('/', ''):
            path = '/index.html'

        rel = path.lstrip('/')
        # Reject path traversal and absolute/backslash tricks.
        if not rel or '..' in rel.split('/') or '\\' in rel or rel.startswith('/'):
            self._send_simple(client, '403 Forbidden', 'Forbidden')
            return

        full = self.www_root + '/' + rel
        try:
            size = os.stat(full)[6]
        except OSError:
            self._send_simple(client, '404 Not Found', 'Not Found')
            return

        ext = rel.rsplit('.', 1)[-1].lower() if '.' in rel else ''
        ctype = _CONTENT_TYPES.get(ext, 'application/octet-stream')

        self._send_head(client, '200 OK', ctype, size, cache=True)
        try:
            with open(full, 'rb') as f:
                while True:
                    chunk = f.read(_FILE_CHUNK)
                    if not chunk:
                        break
                    self._send_all(client, chunk)
        except OSError:
            # Connection dropped mid-transfer, or the file vanished. The headers
            # are already out; nothing useful left to do.
            pass

    # ------------------------------------------------------------- close path
    def _finish(self, client):
        """Response is fully sent. Instead of close()-ing now (which makes us the
        active closer -> TIME_WAIT -> lwIP PCB exhaustion under load), wait for
        the client's FIN in _reap_closing() and close from the passive side."""
        try:
            client.setblocking(False)
        except (AttributeError, OSError):
            pass
        self._closing.append([client, ticks_ms()])

    def _reap_closing(self):
        if not self._closing:
            return
        now = ticks_ms()
        still = []
        for item in self._closing:
            client, started = item
            done = False
            try:
                while True:
                    data = client.recv(128)
                    if data == b'':
                        done = True   # peer FIN
                        break
                    if data is None:
                        break         # would-block: no FIN yet
            except OSError as exc:
                if not _is_eagain(exc):
                    done = True       # error -- just close
            if done or ticks_diff(now, started) > self._close_wait_ms:
                self._hard_close(client)
            else:
                still.append(item)
        self._closing = still

    @staticmethod
    def _hard_close(client):
        try:
            client.close()
        except Exception:
            pass

    # -------------------------------------------------------------- responses
    def _send_head(self, client, status: str, content_type: str, length: int, cache: bool):
        if content_type.startswith(_TEXT_TYPES):
            content_type = content_type + '; charset=utf-8'
        cache_control = 'public, max-age=300' if cache else 'no-store'
        head = (
            'HTTP/1.1 {}\r\n'
            'Content-Type: {}\r\n'
            'Content-Length: {}\r\n'
            'Cache-Control: {}\r\n'
            'Connection: close\r\n'
            '\r\n'
        ).format(status, content_type, length, cache_control)
        self._send_all(client, head.encode('utf-8'))

    def _send_simple(self, client, status: str, text: str):
        body = text.encode('utf-8')
        self._send_head(client, status, 'text/plain', len(body), cache=False)
        self._send_all(client, body)

    @staticmethod
    def _send_all(client, data):
        # socket.send() may only write part of a larger buffer, so loop.
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            n = client.send(view[sent:])
            if not n:
                break
            sent += n
