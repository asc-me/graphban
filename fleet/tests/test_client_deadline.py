"""GRPH-1235: the total deadline binds the response-header phase too."""

import socket
import threading
import time

import httpx
import pytest

from gbfleet.client import _post_within


def _server(behaviour):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    conns = []

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            conns.append(c)
            threading.Thread(target=behaviour, args=(c,), daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return srv, conns


def _run_bounded(url, deadline, limit):
    out = {}

    def go():
        t = time.monotonic()
        try:
            with httpx.Client(timeout=30.0) as client:
                _post_within(client, url, {}, deadline)
        except BaseException as exc:  # noqa: BLE001
            out["exc"] = exc
        out["elapsed"] = time.monotonic() - t

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(limit)
    assert not th.is_alive(), "_post_within hung past its deadline"
    return out


def test_silent_server_times_out_within_deadline():
    srv, _ = _server(lambda c: time.sleep(60))
    try:
        out = _run_bounded(f"http://127.0.0.1:{srv.getsockname()[1]}/", 1, 5)
    finally:
        srv.close()
    assert isinstance(out["exc"], httpx.ReadTimeout)
    assert out["elapsed"] < 2.5


def test_dribbled_headers_time_out_within_deadline():
    def dribble(c):
        try:
            c.sendall(b"HTTP/1.1 200 OK\r\n")
            while True:
                time.sleep(0.2)
                c.sendall(b"X-A: b\r\n")
        except OSError:
            pass

    srv, _ = _server(dribble)
    try:
        out = _run_bounded(f"http://127.0.0.1:{srv.getsockname()[1]}/", 1, 5)
    finally:
        srv.close()
    assert isinstance(out["exc"], httpx.ReadTimeout)
    assert out["elapsed"] < 2.5
