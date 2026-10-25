"""Relay 127.0.0.1:PORT inside the sandbox to the host's ComfyUI (stdlib only).

Astrid's local generation only accepts http://127.0.0.1:<port>, but inside an
OpenShell microVM the host is host.openshell.internal. OpenShell requires each
request's Host to match the connection's destination, and ComfyUI compares
Origin with Host, so the relay rewrites both on every request of a connection
(keep-alive included), forwards bodies by Content-Length or chunked framing,
and pipes raw bytes after a WebSocket upgrade. OpenShell's policy still
inspects and filters every request.

Usage (inside the sandbox): python -I comfyui-relay.py PORT
"""

from __future__ import annotations

import asyncio
import sys

HOST = "host.openshell.internal"
MAX_HEAD = 64 * 1024


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def copy_exact(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, size: int) -> None:
    while size > 0:
        data = await reader.read(min(size, 65536))
        if not data:
            raise ConnectionError("body ended early")
        writer.write(data)
        size -= len(data)
    await writer.drain()


async def copy_chunked(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        line = await reader.readuntil(b"\r\n")
        writer.write(line)
        size = int(line.split(b";", 1)[0].strip(), 16)
        if size == 0:
            while (trailer := await reader.readuntil(b"\r\n")) != b"\r\n":
                writer.write(trailer)
            writer.write(b"\r\n")
            await writer.drain()
            return
        await copy_exact(reader, writer, size + 2)  # chunk data and its CRLF


def rewrite(head: bytes, authority: bytes) -> tuple[bytes, dict[bytes, bytes]]:
    lines = head.split(b"\r\n")
    headers: dict[bytes, bytes] = {}
    out = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(b":")
        key = name.strip().lower()
        headers[key] = value.strip()
        if key == b"host":
            line = b"Host: " + authority
        elif key == b"origin":
            line = b"Origin: http://" + authority
        out.append(line)
    return b"\r\n".join(out) + b"\r\n\r\n", headers


async def requests(client: asyncio.StreamReader, server: asyncio.StreamWriter, authority: bytes) -> None:
    try:
        while True:
            try:
                head = await client.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError:
                return
            if len(head) > MAX_HEAD:
                return
            head, headers = rewrite(head, authority)
            server.write(head)
            if b"upgrade" in headers.get(b"connection", b"").lower():
                await server.drain()
                await pipe(client, server)  # WebSocket from here on
                return
            if b"chunked" in headers.get(b"transfer-encoding", b"").lower():
                await copy_chunked(client, server)
            elif length := int(headers.get(b"content-length", b"0") or b"0"):
                await copy_exact(client, server, length)
            else:
                await server.drain()
    except (asyncio.LimitOverrunError, ConnectionError, OSError, ValueError):
        pass
    finally:
        server.close()


async def handle(port: int, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    try:
        server_r, server_w = await asyncio.open_connection(HOST, port)
    except OSError:
        client_w.close()
        return
    authority = f"{HOST}:{port}".encode()
    await asyncio.gather(requests(client_r, server_w, authority), pipe(server_r, client_w))


async def main(port: int) -> None:
    server = await asyncio.start_server(lambda r, w: handle(port, r, w), "127.0.0.1", port,
                                        limit=MAX_HEAD + 1024)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
