# Evidence bank — Frigate `zmq` detector false-ready after remote peer power loss

**Status:** raw evidence file (internal staging; basis for the public dossier).
**Captured:** 2026-07-25, live during incident response, by the on-host agent.
**Why this file exists:** the originating Frigate log lines rotated out of the
Docker json-file buffer (10MB × 3 cap) within hours — partly *because of* this
bug's own log flood. Every block below was captured verbatim from the live
system before rotation; capture time is noted per block. By 19:09Z the
earliest surviving line in `docker logs frigate` was `03:49:19Z` — the
onset evidence survives only here.

**Sanitization note:** internal RFC1918 addresses are replaced consistently:
`<NVR>` = the Frigate Docker host (LAN address), `<SPARK>` = the NVIDIA DGX
Spark (routed VLAN address). All other content, including every timestamp,
is byte-accurate as captured. All times Zulu unless marked CDT (UTC-5).

---

## 1. Environment

| Item | Value |
|---|---|
| Frigate | `0.17.2-3d4dd3a` (Docker, json-file logging 10m×3) |
| Detector | in-tree plugin `frigate/detectors/plugins/zmq_ipc.py`, `DETECTOR_KEY = "zmq"` |
| Detector config | `type: zmq`, `endpoint: tcp://<SPARK>:5590` (plugin default is `ipc:///tmp/cache/zmq_detector`; defaults `request_timeout_ms: 200`, `linger_ms: 0`) |
| Detector server | house-built ONNX runner (`zmq_onnx_client.py`) in Docker on the DGX Spark (GB10, aarch64), `--restart unless-stopped`, model `yolov9t-uint8.onnx`, CUDAExecutionProvider |
| Healthy baseline | inference_speed ~11–20 ms (measured post-recovery ~11 ms; integration guide's measured figure 10.6 ms; 80 ms+ = documented CPU-fallback signature) |
| Trigger event | severe-storm power interruption at the Spark's site circuit, ~03:25Z 2026-07-25 (22:25 CDT 07-24). The NVR host did not lose power (host + all guests show June boot times). |

Frigate config detector stanza (captured 2026-07-25 ~18:1xZ from the live
config file):

```yaml
detectors:
  spark:
    type: zmq
    endpoint: tcp://<SPARK>:5590
```

Detector server process (captured 2026-07-25 18:35Z, `docker exec ps aux`,
running since container start):

```
PID 1  /venv/bin/python3 detector/zmq_onnx_client.py --endpoint tcp://*:5590 \
       --providers CUDAExecutionProvider CPUExecutionProvider \
       --model /app/models/yolov9t-uint8.onnx
```

## 2. Timeline (all facts below are evidenced in later sections)

| Time (Z) | Event |
|---|---|
| 03:25:58 | First collateral symptom: arbiter's Ollama call to the Spark times out (`<urlopen error timed out>`) — the Spark is going down |
| 03:26:14 | Frigate zmq plugin: `Initializing model: yolov9t-uint8.onnx` (re-init loop begins) |
| 03:26:24 – 03:27:24 | Watchdog "Detection appears to be stuck" restart cycles; `Failed to check and transfer model: Resource temporarily unavailable`; `ZMQ detector ZMQError: Operation cannot be accomplished in current state; resetting socket`; floods of `Model not ready, returning zero detections` (measured 134–238 lines **per second**) |
| **03:28:14** | **`Model yolov9t-uint8.onnx is ready` — final zmq plugin log line for 15.3 hours. The remote host was still down: this "ready" precedes the detector server's existence by 87 seconds.** |
| 03:29:29 | Spark host completes boot (`uptime -s`: 2026-07-24 22:29:29 CDT) |
| 03:29:41 | zmq-detector container starts (Docker `StartedAt`), listens on 5590, prints nothing |
| 03:30:42 | Last visual-object event fleet-wide (arbiter ledger). From here: zero visual detections, streams/recording/audio all healthy |
| 16:02:33 | Daily synthetic known-answer probe (dog clip injected on a test camera) **FAIL at S1-detect: no event created** — first instrument to catch the outage |
| ~17:45 | Human-side investigation begins; `detect_fps=0.0` on every camera, `inference_speed=0.58ms` |
| 18:45:58 | Frigate container restart (the only intervention) |
| 18:46:14 | Real handshake: init → ready in ~180 ms; detection resumes (startup burst 86 detections/s, inference ~11 ms) |
| 18:59:06 | Synthetic probe re-run: ALL PASS (detect 0.94 / recording / snapshot / VLM id) |

Detection blind time: **03:26 → 18:46Z ≈ 15.3 h**, silent — no error state,
no watchdog activity, health endpoints green.

## 3. Frigate log — onset and false ready (captured ~17:52Z, before rotation)

`docker logs frigate --since 2026-07-25T03:00:00`, zmq/watchdog lines:

```
2026-07-25 03:26:14.360807052  [2026-07-25 03:26:14] frigate.detectors.plugins.zmq_ipc INFO    : Initializing model: yolov9t-uint8.onnx
2026-07-25 03:26:24.563829069  [2026-07-25 03:26:24] frigate.watchdog               INFO    : Detection appears to be stuck. Restarting detection process...
2026-07-25 03:26:24.565068763  [2026-07-25 03:26:24] root                           INFO    : Waiting for detection process to exit gracefully...
2026-07-25 03:26:44.390756945  [2026-07-25 03:26:44] frigate.detectors.plugins.zmq_ipc ERROR   : Failed to check and transfer model: Resource temporarily unavailable
2026-07-25 03:26:44.391394862  [2026-07-25 03:26:44] frigate.detectors.plugins.zmq_ipc ERROR   : Failed to initialize model yolov9t-uint8.onnx
2026-07-25 03:26:44.394539624  [2026-07-25 03:26:44] frigate.detectors.plugins.zmq_ipc ERROR   : ZMQ detector ZMQError: Operation cannot be accomplished in current state; resetting socket
2026-07-25 03:26:44.395056135  [2026-07-25 03:26:44] frigate.detectors.plugins.zmq_ipc INFO    : Initializing model: yolov9t-uint8.onnx
2026-07-25 03:26:54.599172122  [2026-07-25 03:26:54] root                           INFO    : Detection process didn't exit. Force killing...
2026-07-25 03:26:54.602500310  [2026-07-25 03:26:54] root                           INFO    : Detection process has exited...
2026-07-25 03:26:54.640681949  [2026-07-25 03:26:54] frigate.detectors.plugins.zmq_ipc INFO    : Initializing model: yolov9t-uint8.onnx
2026-07-25 03:27:04.641432993  [2026-07-25 03:27:04] frigate.watchdog               INFO    : Detection appears to be stuck. Restarting detection process...
2026-07-25 03:27:04.647995158  [2026-07-25 03:27:04] root                           INFO    : Waiting for detection process to exit gracefully...
2026-07-25 03:27:24.668710530  [2026-07-25 03:27:24] frigate.detectors.plugins.zmq_ipc ERROR   : Failed to check and transfer model: Resource temporarily unavailable
2026-07-25 03:27:24.669070001  [2026-07-25 03:27:24] frigate.detectors.plugins.zmq_ipc ERROR   : Failed to initialize model yolov9t-uint8.onnx
2026-07-25 03:27:24.679610308  [2026-07-25 03:27:24] frigate.detectors.plugins.zmq_ipc WARNING : Model not ready, returning zero detections
[... repeats at 134-238 lines/second ...]
```

Per-second message histogram at the end of the flood, and the final lines
(captured ~18:05Z via `--since 2026-07-25T03:28:00`, dedup by second):

```
    134 03:28:04  WARNING : Model not ready, returning zero detections
    238 03:28:05  WARNING : Model not ready, returning zero detections
    182 03:28:06  WARNING : Model not ready, returning zero detections
    174 03:28:07  WARNING : Model not ready, returning zero detections
    159 03:28:08  WARNING : Model not ready, returning zero detections
    141 03:28:09  WARNING : Model not ready, returning zero detections
    146 03:28:10  WARNING : Model not ready, returning zero detections
     74 03:28:11  WARNING : Model not ready, returning zero detections
     60 03:28:12  WARNING : Model not ready, returning zero detections
     68 03:28:13  WARNING : Model not ready, returning zero detections
     50 03:28:14  WARNING : Model not ready, returning zero detections
      1 03:28:04  ERROR   : Failed to check and transfer model: Resource temporarily unavailable
      1 03:28:04  ERROR   : Failed to initialize model yolov9t-uint8.onnx
      1 03:28:14  INFO    : Initializing model: yolov9t-uint8.onnx
      1 03:28:14  INFO    : Model yolov9t-uint8.onnx is ready
```

```
2026-07-25 03:28:14.764743598  [...] zmq_ipc WARNING : Model not ready, returning zero detections
2026-07-25 03:28:14.765705270  [...] zmq_ipc WARNING : Model not ready, returning zero detections
2026-07-25 03:28:14.781654191  [...] zmq_ipc WARNING : Model not ready, returning zero detections
2026-07-25 03:28:14.782761333  [...] zmq_ipc WARNING : Model not ready, returning zero detections
2026-07-25 03:28:14.858317086  [2026-07-25 03:28:14] frigate.detectors.plugins.zmq_ipc INFO    : Initializing model: yolov9t-uint8.onnx
2026-07-25 03:28:14.863632178  [2026-07-25 03:28:14] frigate.detectors.plugins.zmq_ipc INFO    : Model yolov9t-uint8.onnx is ready
```

**After 03:28:14 the zmq plugin and detection watchdog logged NOTHING until
the 18:45:58Z manual restart.** (Verified ~18:05Z: zero matching lines
between 03:28:15 and the capture moment.)

Note the init→"ready" interval on the false flip: **5.3 ms**
(03:28:14.858 → 03:28:14.863) — compare the genuine post-restart handshake
(§7): ~180 ms.

## 4. The ready flip preceded the server's start

Spark host boot time (captured 18:34Z over SSH):

```
SPARK booted: 2026-07-24 22:29:29        # = 2026-07-25 03:29:29 Z (host is CDT)
```

Detector container state (captured 18:35Z, `docker inspect`):

```
started=2026-07-25T03:29:41.015417488Z restarts=0 status=running oom=false
```

**"Model ready" was logged at 03:28:14.863Z — 75 s before the host finished
booting and 87 s before the detector container started.** No process that
could legitimately answer the model-ready handshake existed at that moment.
(Both machines are NTP-synced; the margin is far beyond clock skew.)

Post-reboot detector server log — **empty** (captured 18:35Z):

```
$ docker logs -t --since 2026-07-25T03:29:00Z zmq-detector
(no output)
```

(Caveat, noted for fairness: the server's Python process may not run
unbuffered, so its post-boot stdout could be sitting in a buffer. The
server WAS listening: `/proc/net/tcp` inside the container showed
`00000000:15D6` (0.0.0.0:5590) in LISTEN, and the GPU (GB10) was visible
to it. Its health at that point is *unproven either way*; what is proven
is that Frigate's "ready" predates the server's existence.)

## 5. The silent-failure state (captured ~17:45–18:20Z, pre-restart)

Frigate `/api/stats` while wedged:

```
detector spark: inference_speed = 0.58 ms   detection_start = 0.0   (pid 937129)
cam109  camera_fps = 5.0  detect_fps = 0.0
cam117  camera_fps = 6.0  detect_fps = 0.0
cam120  camera_fps = 5.0  detect_fps = 0.0
cam14   camera_fps = 6.0  detect_fps = 0.0
camtest camera_fps = 5.1  detect_fps = 0.0
service uptime = 100864 s   version = 0.17.2-3d4dd3a
```

The 0.58 ms average is below the bare wire round trip between the hosts
(0.9 ms measured ping), before any inference work (healthy measured
baseline ~11 ms, §7). It is consistent with
requests being answered (or short-circuited) with empty results ~instantly
— while streams, recording, and audio detection continued normally, so
every coarse health signal stayed green.

Frigate `/api/events?limit=5` at ~18:10Z — newest events are audio-only;
NO visual object events after 03:30Z:

```
audio142 door 16:31:36Z · cam2(audio-relay) door 16:31:36Z · audio109 bird 15:43:51Z · audio109 bird 15:15:14Z · audio109 bird 14:38:13Z
```

Arbiter (downstream judge) ledger, fleet-wide: last visual-event rows at
03:30:42Z; the only anomalies at onset are two rows `action=error`,
`error=<urlopen error timed out>` at 03:25:58Z and 03:28:22Z (its VLM calls
to the same dying host).

## 6. Independent detection proof: synthetic known-answer probe

Daily probe injects a dog clip into a dedicated test camera and asserts
4 stages (detect → recording → snapshot → VLM identification). Ledger rows:

```
07-24 14:35:08Z  verdict=ALL PASS  S1 score=0.94  (76.8 s)   # pre-outage
07-24 16:01:39Z  verdict=ALL PASS  S1 score=0.94  (97.0 s)   # pre-outage, cron
07-25 16:02:33Z  verdict=FAIL at S1-detect  event_id=null    # DURING outage:
                 S1=false, S2/S3/S4 skipped (no event)  (151.6 s)
07-25 18:59:06Z  verdict=ALL PASS  S1 score=0.94  (124 s)    # post-restart verify
```

## 7. Recovery — single intervention, Frigate-side only

Frigate container restarted 18:45:58Z. The detector server on the Spark was
**not** touched (same PID-1 process from 03:29:41Z). Handshake log:

```
2026-07-25 18:46:10.118  zmq_ipc INFO : Initializing model: yolov9t-uint8.onnx
2026-07-25 18:46:10.122  zmq_ipc INFO : Model yolov9t-uint8.onnx is ready
2026-07-25 18:46:14.347  zmq_ipc INFO : Initializing model: yolov9t-uint8.onnx
2026-07-25 18:46:14.525  zmq_ipc INFO : Model yolov9t-uint8.onnx is ready   # ~180 ms — a real round trip
```

Stats shortly after (startup motion burst): `detection_fps total = 86.0`,
`inference_speed = 10.96 ms`, all cameras streaming. Probe ALL PASS at
18:59:06Z (§6). Recovery via Frigate restart alone **proves the server had
been healthy and reachable the whole time** — the wedge was entirely
client-side state.

## 8. Proven vs. hypothesized

**Proven by the artifacts above:**
1. The zmq plugin declared "Model ready" while the remote endpoint
   demonstrably did not exist (87 s before container start; 75 s before
   host boot completion).
2. From that moment, all object detection silently returned zero for
   15.3 h: no errors, no watchdog action, `detect_fps=0`, no visual events,
   implausible 0.58 ms inference average — while streams/recording/audio
   stayed healthy.
3. Restarting only the Frigate container restored detection against the
   untouched server → the persistent failure state lived in the Frigate
   (client) side.
4. The false ready-flip completed in 5.3 ms vs ~180 ms for a genuine
   handshake.
5. Failure-loop side effect: `Model not ready` logged at 134–238
   lines/second, which rotated the container's entire log history
   (10MB×3 json-file) within hours, destroying unrelated forensic
   evidence.

**Hypothesized (plausible mechanism, NOT proven):**
- Mechanism of the false flip: after repeated `ZMQError ... resetting
  socket` cycles on the REQ/REP pair, a stale queued reply (from a
  pre-outage request) or a reconnect-buffered message satisfied the
  ready/check-model exchange once TCP came back mid-boot — i.e., the
  ready-check trusts a socket-level success that does not prove a live,
  model-loaded peer. Plugin defaults suggest a local-IPC design context
  (`ipc:///tmp/cache/...`, 200 ms timeout) where a vanished peer is far
  less likely than over TCP to a separately-powered host.
- The 0.58 ms replies: consistent with either plugin-internal
  short-circuit or trivial empty responses from the server; not
  distinguished (server-side logging was silent, see §4 caveat).

**Code corroboration (added 2026-07-25 after reading `zmq_ipc.py` at
`v0.17.2` and `dev` — these are facts about the code, supporting but not
proving the mechanism above):**
- The ready decision is a single request/reply in
  `_check_and_transfer_model()`: send `{"model_request": true,
  "model_name": ...}`, accept any single-frame JSON reply with
  `model_available && model_loaded`. **No nonce / correlation id** ties
  the reply to the request or to the current socket generation — a stale
  or misaligned reply is indistinguishable from a genuine one.
- `_model_ready` is set once and **never re-verified**; the detect path
  only reads the flag. No periodic re-check, no re-handshake trigger on
  consecutive empty results.
- ZMQ `connect()` is asynchronous: sends toward a dead peer queue
  optimistically and succeed locally, so send-success proves nothing.
  The anomaly needing explanation is therefore ONLY the 5.3 ms *receipt*
  of a valid-looking reply — which the missing correlation id would let
  through regardless of its true origin.
- `dev` (0.18 line) differs from `v0.17.2` only in typing/pydantic
  metadata; ready logic behaviorally identical — the defect surface
  persists in the next release line.

## 9. Mitigations deployed on our side (context for "impact/workaround")

- Canary (external watchdog, 30-min cron) gained two detection-liveness
  checks the same day: (a) detector `inference_speed` sanity band — below
  3 ms = junk-reply signature → alert (would have fired ~34 min into this
  incident); (b) daylight-weighted visual-event drought vs. a daily
  synthetic known-answer event → alert (cause-agnostic backstop; would
  have fired by hour 3 of daylight). Backtested against this incident's
  real window: both fire.
- Suggested plugin-side fixes (for the upstream discussion): ready-state
  must be proven by a live round-trip (e.g., a test inference or a
  nonce'd model-hash exchange) rather than socket-level success; and the
  `Model not ready` warning needs rate-limiting (one line/sec with a
  suppressed-count would have preserved ~200× more log history).
