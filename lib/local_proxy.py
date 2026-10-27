# -*- coding: utf-8 -*-
"""Minimal HTTP CONNECT proxy that binds its outbound sockets to a chosen local NIC.
Usage: python local_proxy.py <BIND_IP|-> [PORT]
Lets Chrome/Electron reach CN sites through the home broadband instead of the system proxy.
"""
import socket, threading, sys

BIND = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != '-' else None
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8899


def relay(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass


def handle(conn):
    try:
        conn.settimeout(20)
        data = b''
        while b'\r\n\r\n' not in data:
            c = conn.recv(4096)
            if not c:
                conn.close()
                return
            data += c
        head = data.split(b'\r\n\r\n', 1)[0].decode('latin1')
        parts = head.split('\r\n')[0].split(' ')
        if len(parts) < 3:
            conn.close()
            return
        method, target = parts[0], parts[1]
        if method.upper() != 'CONNECT':
            conn.sendall(b'HTTP/1.1 501 Not Implemented\r\n\r\n')
            conn.close()
            return
        host, _, port = target.rpartition(':')
        up = socket.socket()
        if BIND:
            up.bind((BIND, 0))
        up.settimeout(20)
        up.connect((host, int(port)))
        up.settimeout(None)
        conn.settimeout(None)
        conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        threading.Thread(target=relay, args=(conn, up), daemon=True).start()
        relay(up, conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', PORT))
srv.listen(100)
print('proxy listening on 127.0.0.1:%d bind=%s' % (PORT, BIND), flush=True)
while True:
    c, _ = srv.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
