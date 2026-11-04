# Live ComfyUI capture, 423cbd20

The JSON files and SHA-256 manifest are the S-45 live runner's non-private
captures, copied without alteration. `used-nodes.json` is the runner's
projection of `/object_info` to nodes used by the finished prompt.
`history.json`, `queue.json`, and `system-stats.json` are response bodies;
`view-headers.json` and `free-response.json` capture status and headers/body.
The image bytes in the CPU replay are synthetic. The original prompt submission
response and websocket frames were not retained; see `unavailable.json`.

BUG-15 delivery replay: `qwen-delivery-nodes.json` contains unmodified records
for EmptyLatentImage, QwenImage21Cache and ManualSigmas from the retained full
`GPU-SHAKEDOWN/out/live/live-object-info.json` capture (SHA-256
`2b2aaa7ab2a9ed0242aae0d6873fa0bd7bf188ce96fbb22e8dd3cbf8836e6a99`).
These classes occur in the captured history and pinned Qwen template but were
omitted from the original `used-nodes.json` projection. The supplement SHA-256
is `6e37fb380f18b079e25ef3c499b2b4238211c19ab83e7549e453d10a0f7277ad`.
