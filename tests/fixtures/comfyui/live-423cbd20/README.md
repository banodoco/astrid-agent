# Live ComfyUI capture, 423cbd20

The JSON files and SHA-256 manifest are the S-45 live runner's non-private
captures, copied without alteration. `used-nodes.json` is the runner's
projection of `/object_info` to nodes used by the finished prompt.
`history.json`, `queue.json`, and `system-stats.json` are response bodies;
`view-headers.json` and `free-response.json` capture status and headers/body.
The image bytes in the CPU replay are synthetic. The original prompt submission
response and websocket frames were not retained; see `unavailable.json`.
