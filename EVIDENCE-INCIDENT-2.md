# Evidence bank — Incident 2: same silent wedge, EAGAIN on a live re-init (no reboot involved)

**Status:** raw evidence file (internal staging; companion to `EVIDENCE.md`,
which documents Incident 1 of 2026-07-25).
**Captured:** 2026-07-27/28, live during incident response, by the on-host
agent. Unlike Incident 1, the raw log excerpts here were transcribed into the
session's recovery report at capture time rather than banked as full verbatim
dumps; every figure below traces to that live capture.
**Why this file exists:** two days after Incident 1, the same plant hit the
same wedge state through a **different trigger** — no power event, no server
reboot, no Frigate restart. A second, independent path into the identical
no-recovery state shows the defect is not tied to a cold-start race.

**Sanitization note:** as in `EVIDENCE.md`: `<NVR>` = the Frigate Docker
host (LAN address), `<SPARK>` = the NVIDIA DGX Spark (routed VLAN address).
All times Zulu.

---

## 1. Environment

Identical plant to Incident 1 (`EVIDENCE.md` §1): Frigate `0.17.2-3d4dd3a`
in Docker, in-tree `zmq_ipc` plugin, `type: zmq`,
`endpoint: tcp://<SPARK>:5590`, house-built ONNX detector server on the DGX
Spark (GB10, aarch64), model `yolov9t-uint8.onnx`. Healthy reference
inference_speed ~10–12 ms; wire RTT ~0.9 ms.

Key difference from Incident 1's conditions:

| Item | Incident 1 (07-25) | Incident 2 (07-27) |
|---|---|---|
| Power event | Yes — storm cut power to the Spark's circuit | **None** |
| Server reboot | Yes (4 min) | **None** — detector server process uptime 2d23h across the whole incident (same PID-1 since 07-25 03:29:41Z) |
| Frigate restart preceding onset | No | No |
| Trigger of the plugin's model (re)init | Peer vanished during reboot | **Undetermined** (see §6) |

## 2. Timeline

| Time (Z) | Event |
|---|---|
| 2026-07-27 21:52:06 | `zmq_ipc` logs `Failed to check and transfer model: Resource temporarily unavailable` + `Failed to initialize model yolov9t-uint8.onnx` — then **silence**. Wedge begins. |
| 21:52 → 02:31 (+1d) | Zero object events fleet-wide, ~4.5 h. All streams up, recording healthy, UI/API green. Audio-based detection (which does not ride this plugin) kept producing events throughout. |
| ~02:1x | Post-maintenance load check notices GPU utilization far below the plant's normal detector floor; external liveness check run (see §4). |
| ~02:31 | `docker restart frigate` on `<NVR>` — the only intervention. Spark server untouched. (Exact restart timestamp not banked; bounded by the outage window end and the 02:32:12Z handshake.) |
| 2026-07-28 02:32:12 | Genuine handshake: `Initializing model: yolov9t-uint8.onnx` → `Model yolov9t-uint8.onnx is ready`. inference_speed 10.0–12.09 ms immediately. |
| 02:35:39 | Live end-to-end proof: person walk-test → event created and finalized (cam14, person 0.76). |

## 3. Onset — the wedge's birth certificate (Frigate container log)

The plugin attempted a model re-initialization mid-operation and logged
exactly two lines:

```
2026-07-27 21:52:06  zmq_ipc ERROR : Failed to check and transfer model: Resource temporarily unavailable
2026-07-27 21:52:06  zmq_ipc ERROR : Failed to initialize model yolov9t-uint8.onnx
```

`Resource temporarily unavailable` is the EAGAIN error code — a transient,
explicitly retryable condition (here: the zmq receive timed out; the server
WAS listening throughout, see §4). After these two lines: nothing. No retry,
no crash, no unhealthy state, no watchdog action — the same terminal
silence as Incident 1.

Note the contrast with Incident 1's onset: no log flood this time, and no
false `Model ... is ready` line — the plugin wedged directly from the failed
init. Two different onset shapes, one identical end state.

## 4. The silent-failure state (captured live, pre-restart)

Frigate side:

- Reported detector `inference_speed`: **0.35 ms** average — below the
  0.9 ms wire RTT between the hosts, i.e. physically impossible for real
  inference; the junk-reply signature from Incident 1 (there: 0.58 ms).
- Zero visual object events across all cameras for ~4.5 h while
  `camera_fps` stayed normal on every camera.
- All UI/API health signals green throughout.

Server side (checked during the wedge):

- Detector port 5590 in LISTEN, but **zero established connections** from
  the Frigate host.
- GPU utilization 1–7% vs the ~26% floor normal for this camera load.
- Detector server process untouched and up 2d23h — never restarted before,
  during, or after the incident.

External two-signal liveness check (the `tools/detect-liveness-check.py`
published in this repo after Incident 1) returned double CRIT when run:

```
CRIT inference_speed 0.35ms < 3.0ms floor (junk-reply signature)
CRIT event drought: 3.6 counted hours ... last event 2026-07-27 21:25Z
```

Both external signals fired for both incidents — the detection signature
generalizes across the two trigger paths.

## 5. Recovery — Frigate-side restart only, end-to-end verified

- `docker restart frigate` on `<NVR>`; the Spark server was not touched.
- Genuine handshake at 02:32:12Z (`Initializing model:` →
  `Model yolov9t-uint8.onnx is ready`); inference_speed back to
  10.0–12.09 ms (healthy reference ~11 ms); server GPU utilization back to
  21–30% (normal floor restored).
- Live walk test 3 minutes later: person event created and finalized
  (02:35:39Z, score 0.76).
- Liveness checker: `PASS detection alive (spark=12.09ms; 5/5 streams up)`,
  exit 0.

As in Incident 1, recovery via a Frigate-only restart against the untouched
server proves the persistent failure state lived entirely on the Frigate
(client) side.

## 6. Proven vs. undetermined

**Proven by the artifacts above:**
1. A mid-operation model re-init failed with EAGAIN and the plugin entered
   the identical no-recovery state as Incident 1: zero detections, junk
   inference_speed (0.35 ms), no errors after the initial two lines, no
   watchdog action, all health signals green, for ~4.5 h.
2. No power event, no server restart, and no Frigate restart preceded the
   onset — this is a second, independent path into the wedge, ruling out
   "cold-start race after peer reboot" as the sole arming condition.
3. The server was listening but had zero established connections from
   Frigate during the wedge, and its GPU sat at 1–7% vs a ~26% normal
   floor — no inference was being requested of it.
4. A Frigate-only restart fully restored detection, verified end-to-end
   with a live walk-test event within 3 minutes.
5. Audio-based detection continued producing events throughout — a
   false-comfort signal that masks this outage from casual observation.

**Undetermined (recorded as unknown, not assumed):**
- **What triggered the model re-init at 21:52:06Z.** Something restarted
  Frigate's detector process mid-operation; the cause was not established.
  Plant context, for completeness: roughly 45 minutes earlier, an unrelated
  memory-exhaustion fault on the Spark host (a leaking scoring process,
  independent of the detector server) had been remedied; no causal chain
  between that work and the 21:52Z re-init was established in either
  direction. Whatever the trigger, any legitimate re-init that hits a
  transient EAGAIN lands in the same unrecoverable state.
- **Why the zmq receive timed out** at that moment. The server host had
  been under severe memory pressure earlier that evening (resolved before
  onset); transient unresponsiveness is plausible but was not proven.
