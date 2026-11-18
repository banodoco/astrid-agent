"""Finite CPU Comfy peer launched by the real managed engine supervisor."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from aiohttp import web
from PIL import Image

port = int(sys.argv[1])
started = Path(sys.argv[3])
with started.open("a") as stream:
    stream.write(str(os.getpid()) + "\n")
history, sockets = {}, {}
schema = {name: {} for name in ("MiniMaxH3ReferenceToVideo", "UNETLoader", "VAELoader", "VHS_VideoCombine")}
schema.update({
    "EmptyImage": {"input": {"required": {"width": ["INT", {"default": 16}], "height": ["INT", {"default": 16}],
        "batch_size": ["INT", {"default": 1}], "color": ["INT", {"default": 0}]}},
        "output": ["IMAGE"], "output_name": ["IMAGE"], "output_node": False, "python_module": "nodes"},
    "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {"default": "astrid"}]}},
        "output": [], "output_node": True, "python_module": "nodes"},
})


async def complete(prompt, client, record):
    await asyncio.sleep(0.025)
    socket = sockets[client]
    await socket.send_json({"type": "execution_success", "data": {"prompt_id": prompt}})
    history[prompt] = record
    await socket.send_json({"type": "executing", "data": {"prompt_id": prompt, "node": None}})


async def handle(request):
    with (started.parent / "requests.jsonl").open("a") as stream:
        stream.write(json.dumps([request.method, request.path]) + "\n")
    if request.path == "/ws":
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        client = request.query["clientId"]
        sockets[client] = socket
        await socket.send_json({"type": "status", "data": {}})
        try:
            async for _ in socket:
                pass
        finally:
            sockets.pop(client, None)
        return socket
    if request.path == "/object_info":
        return web.json_response(schema)
    if request.path == "/system_stats":
        return web.json_response({"system": {"comfyui_version": "0.36.0"}, "devices": [{"vram_free": 1024}]})
    if request.path == "/queue":
        return web.json_response({"queue_running": [], "queue_pending": []})
    if request.path.startswith("/history/"):
        prompt = request.path.rsplit("/", 1)[1]
        return web.json_response({prompt: history[prompt]} if prompt in history else {})
    if request.path == "/view":
        return web.Response(body=(started.parent / request.query["filename"]).read_bytes(), content_type="image/png")
    if request.path == "/prompt":
        body = await request.json()
        assert body["client_id"] in sockets
        prompt = uuid.uuid4().hex
        filename = prompt + ".png"
        Image.new("RGB", (16, 16), (10, 20, 30)).save(started.parent / filename)
        outputs = {key: {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}
                   for key, value in body["prompt"].items() if value["class_type"] == "SaveImage"}
        record = {"outputs": outputs, "status": {"completed": True, "status_str": "success"}}
        asyncio.create_task(complete(prompt, body["client_id"], record))
        with (started.parent / "prompts.jsonl").open("a") as stream:
            stream.write(json.dumps(body) + "\n")
        return web.json_response({"prompt_id": prompt, "number": 1, "node_errors": {}})
    return web.json_response({})


app = web.Application()
app.router.add_route("*", "/{path:.*}", handle)
web.run_app(app, host="127.0.0.1", port=port, print=None)
