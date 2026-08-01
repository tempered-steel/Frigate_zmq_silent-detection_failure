# Frigate `zmq` detector: false "Model ready" after remote peer power loss → 15 h of silent zero-detections

An evidence dossier for an incident in which Frigate's in-tree ZMQ detector
plugin (`frigate/detectors/plugins/zmq_ipc.py`) declared its model ready
**87 seconds before the remote detector process existed**, then silently
returned zero detections for 15.3 hours — with every health signal green.

**Incidents:** #1 2026-07-25 (power loss at the peer, 15.3 h) · #2
2026-07-27 (EAGAIN on a live re-init, no reboot involved, ~4.5 h) ·
**Frigate:** `0.17.2-3d4dd3a` (defect verified present on `dev` too) ·
**Detector:** `type: zmq` over `tcp://` to a network
host (NVIDIA DGX Spark, GB10) · **Keywords:** Frigate zmq detector, silent
zero detections,
Model not ready returning zero detections, false model ready, detect_fps 0,
network detector, REQ/REP, EAGAIN, NVR silent failure, detection watchdog ·
**Companion repo:** [frigate-dgx-spark](https://github.com/tempered-steel/frigate-dgx-spark)
(the integration this bit) · **Full artifacts:** [`EVIDENCE.md`](EVIDENCE.md)
(#1) · [`EVIDENCE-INCIDENT-2.md`](EVIDENCE-INCIDENT-2.md) (#2) ·
**[Are you affected? ↓](#are-you-affected-the-60-second-check)**

---

> **Update 2026-07-28 — second, independent occurrence.** Two days after
> the incident below, the same plant entered the identical wedge state
> through a **different trigger**: a mid-operation model re-init failed
> with EAGAIN (`Resource temporarily unavailable`) and the plugin went
> silent — no power event, no server reboot, no Frigate restart preceding
> it; the detector server process was up 2d23h across the whole incident.
> Zero detections for ~4.5 h with the same junk inference_speed signature
> (0.35 ms), same green health signals, same Frigate-only-restart remedy.
> The wedge does not need a reboot or a vanished peer to arm — any failed
> (re)init lands in the same unrecoverable state. Full artifacts:
> [`EVIDENCE-INCIDENT-2.md`](EVIDENCE-INCIDENT-2.md).

## The short version

A storm cut power to the machine serving our ZMQ detector. It rebooted and
recovered in ~4 minutes. Frigate did not: after two minutes of watchdog
restart cycles and socket resets against the dead endpoint, the plugin
logged `Model yolov9t-uint8.onnx is ready` — at a moment when, provably, no
process capable of answering that handshake existed. From then on:

- every detection request returned zero objects, **silently** — no errors,
  no watchdog action, no log lines at all;
- `detect_fps` sat at 0.0 on all cameras while streams and recording ran
  normally, so coarse health checks stayed green;
- the detector's reported `inference_speed` averaged **0.58 ms** — below
  the bare wire round trip between the hosts (0.9 ms measured ping),
  before any inference work; the measured healthy baseline is ~11 ms;
- the state persisted 15.3 hours until a manual restart of the Frigate
  container. The remote detector server was **never touched** — it had
  auto-started at boot and was healthy the whole time. The wedge was
  entirely client-side.

Three recorded timestamps define the finding:

| Time (Z), 2026-07-25 | Fact |
|---|---|
| 03:28:14.863 | Frigate logs `Model ... is ready` (last plugin log line for 15.3 h) |
| 03:29:29 | detector **host** finishes booting (`uptime -s`) |
| 03:29:41 | detector **container** starts (Docker `StartedAt`, `restarts=0`) |

Both machines are NTP-synced; the margin is far beyond clock skew. A
second signature points the same way: the false init→ready flip took
**5.3 ms**; the genuine handshake after the manual restart took ~180 ms.

Full timeline, verbatim logs, container/host state captures, and the
recovery record are in [`EVIDENCE.md`](EVIDENCE.md).

## Are you affected? The 60-second check

Three reads against Frigate's API. Any one failing verify-line means you
are in (or near) this failure state right now.

```bash
FRIGATE=http://your-frigate-host:5000

# 1. Implausible inference speed (the wedge signature)
curl -s $FRIGATE/api/stats | jq '.detectors'
# verify: inference_speed is a sane number for your hardware
#   (golden reference: ~11ms GPU over LAN).
# Too slow: 80ms+ suggests silent CPU fallback.
# Too fast: below ~3ms is replies without work, not inference — wedged.

# 2. Detection dead while streams are alive
curl -s $FRIGATE/api/stats | jq '.cameras | to_entries[] |
  {cam: .key, camera_fps: .value.camera_fps, detect_fps: .value.detection_fps}'
# verify: cameras with healthy camera_fps also show detect_fps > 0
# when things move. Every camera stuck at detect_fps 0.0 for hours,
# with camera_fps healthy, = detection is not running.

# 3. Event silence you can't explain
curl -s "$FRIGATE/api/events?limit=5" | jq '.[] | {camera, label, start_time}'
# verify: recent object events exist for cameras that see activity;
# none during active hours = wedged.
```

Automated version: [`tools/detect-liveness-check.py`](tools/detect-liveness-check.py)
(stdlib-only, cron-able, exit codes for your alerting — see
[Workarounds](#workarounds-we-run-meanwhile-external-no-frigate-patch)).

## Why this matters

`type: zmq` is the supported path for running detection *outside* the
Frigate container — which increasingly means on a **separate machine**
(that is exactly what our [companion integration guide](https://github.com/tempered-steel/frigate-dgx-spark)
documents, and what GB10-class boxes invite). A separately-powered peer
can vanish and return at any time. If a peer bounce can leave the plugin
in a permanent false-ready state, an NVR whose entire purpose is
unattended vigilance fails **silent and open**, indefinitely, while
looking healthy. We only caught it because an independent synthetic
known-answer probe (a daily injected test clip asserting
detect→record→snapshot→identify) failed its detection stage.

## What is proven vs. hypothesized

**Proven** (artifacts in [`EVIDENCE.md`](EVIDENCE.md)):
1. "Ready" was declared while the remote endpoint demonstrably did not
   exist.
2. Detection then silently returned zero for 15.3 h (no errors, no
   watchdog restarts, implausible 0.58 ms inference average).
3. A Frigate-only restart against the untouched server restored service —
   the persistent failure state was client-side.
4. The defect surface is unchanged on the `dev` branch: `zmq_ipc.py`
   there differs from 0.17.2 only cosmetically (typing/metadata); the
   ready logic is behaviorally identical.

**Code-supported analysis** (from reading `zmq_ipc.py`, 0.17.2 and `dev`):
- The entire ready decision is one request/reply:
  `_check_and_transfer_model()` sends `{"model_request": true, ...}` and
  accepts any single-frame JSON reply with
  `model_available && model_loaded` — **the reply carries no nonce or
  correlation id**, so nothing ties it to the request just sent, or even
  to this socket generation.
- `_model_ready` is **set once and never re-verified**: the detect path
  only reads the flag. There is no periodic re-check and no
  "N consecutive empty results" trigger that would re-handshake.
- ZMQ's asynchronous `connect()` means sends toward a dead peer can
  succeed locally (messages queue optimistically) — so send-success
  proves nothing about peer liveness.

**Hypothesized** (labeled as such): the specific 5.3 ms "reply" that
flipped ready during our incident was a stale queued reply misaligned
across the plugin's socket-reset choreography (repeated
`ZMQError: Operation cannot be accomplished in current state; resetting
socket` cycles on a strict REQ/REP pair). The missing correlation id is
exactly why nothing could catch the misalignment. The precise queue
mechanics need a maintainer's eye or a sandboxed repro; the forensics
prove *that* it happened, the code shows *where* it is possible.

## Suggested fix direction

Ready-state should be earned by a **live round trip that only a working
peer can complete**, and kept honest afterward:

- correlate request and reply (a nonce in the model-check exchange);
- prove liveness with work, not headers — a test inference on a known
  tensor, or a nonce'd model-hash exchange;
- re-verify periodically, or re-handshake after N consecutive empty
  results, so a wedge self-heals instead of persisting indefinitely.

A secondary harm worth weighing in the same review: during the two-minute
failure loop the plugin logged `Model not ready, returning zero
detections` at a measured **134–238 lines/second** (one per detection
request). Under Docker's default json-file caps that flood destroyed the
container's entire prior log history within hours — including the
forensic window for an unrelated failure we were investigating. Whether
that warrants throttling (e.g., once per second with a suppressed-count)
we leave to the maintainers' bigger picture.

## Workarounds we run meanwhile (external, no Frigate patch)

- **[`tools/detect-liveness-check.py`](tools/detect-liveness-check.py)** —
  the generic, cron-able version of both checks below. Stdlib only,
  exit codes 0/1/2 for whatever alerting you already trust. Backtested
  against this incident's real window: fires at the first run after the
  wedge (~30 minutes in).
- **Inference-speed sanity band**: `inference_speed` below 3 ms = junk-
  reply signature → alert. Above ~70 ms = silent-CPU-fallback suspect.
- **Visual-event drought**: hours without any object event while camera
  streams are demonstrably up → alert. Weight the clock to your active
  hours (quiet nights are normal) — see the tool's `--daylight-utc`.
- **Synthetic known-answer probe**: daily injection of a labeled clip
  into a dedicated test camera, asserting the full
  detect→record→snapshot→identify chain. This was the instrument that
  caught the incident; including the probe camera in the drought check
  makes it nearly false-positive-free.

## FAQ

**Am I affected if I use the default local IPC endpoint
(`ipc:///tmp/cache/...`) instead of TCP?**
Same code path, so the design gap exists there too, but the trigger is
much less likely: a local detector process is typically supervised
alongside Frigate and doesn't "vanish and return" the way a
separately-powered network host does. Our incident and evidence are
TCP-specific. Unproven either way for IPC.

**Does 0.18 / `dev` fix it?**
No. We diffed `zmq_ipc.py` between `v0.17.2` and `dev`: only typing and
pydantic-metadata changes; the ready logic is behaviorally identical.

**My detector host was rebooted/replaced — does restarting the detector
server fix a wedged Frigate?**
No, and this is the operationally important fact: the failure state
lives **client-side**, in Frigate. In our incident the server had been
healthy for 15 hours while Frigate stayed wedged. **Restart the Frigate
container** (or its detection process); the server needs nothing.

**Why didn't Frigate's own watchdog catch it?**
It was doing its job *before* the false flip — we captured repeated
"Detection appears to be stuck. Restarting detection process..." cycles
while the peer was down. The false "ready" ended those restarts: with
`_model_ready = True` and requests returning (empty) results instantly,
nothing looked stuck to the watchdog anymore. The flag silenced exactly
the mechanism that was trying to recover.

**Is this the apple-silicon-detector server's fault?**
No. The server (we run a derivative of
[frigate-nvr/apple-silicon-detector](https://github.com/frigate-nvr/apple-silicon-detector))
auto-started cleanly at boot, was listening, and served the genuine
handshake instantly when Frigate was restarted — untouched — 15 hours
later. Separately, that project's issue #27 (explicit-mode
`model_ready`, `--providers` handling) was fixed upstream in July 2026;
those server-side fixes are unrelated to this client-side defect.

**What can trigger it besides a power loss?**
Anything that makes the peer unreachable — or merely slow to answer —
during the (re-)handshake window: host reboots, container restarts, link
flaps, switch reboots, VLAN hiccups, or a peer host too busy to reply
within the plugin's 200 ms timeout. No longer hypothetical: our second
incident ([`EVIDENCE-INCIDENT-2.md`](EVIDENCE-INCIDENT-2.md)) armed the
wedge with **no reboot of anything** — a live mid-operation re-init hit
EAGAIN against a server that was up and listening the whole time. A power
event is just the version with the best forensic timestamps.

**How do I know it happened to me historically?**
Frigate logs to stdout — search it with
`docker logs frigate 2>&1 | grep zmq_ipc` (or the System → Logs page in
the web UI; Home Assistant add-on users: the add-on Log tab). Look for a
`Model ... is ready` line during a window when the detector endpoint was
down, followed by an absence of zmq/watchdog log lines and an
event-free gap on active cameras. Note the log-flood
caveat: the failure loop may have rotated your logs (ours were gone in
hours under default Docker json-file caps).

## Appendix A — Terms

| Term | Meaning here |
|---|---|
| **ZMQ REQ/REP** | ZeroMQ's strict request/reply socket pair: one send must be answered by one recv, in order. State violations raise `Operation cannot be accomplished in current state`. |
| **false ready** | The plugin's `_model_ready = True` state reached without a live, model-loaded peer — this repo's subject. |
| **`camera_fps` vs `detect_fps`** | Frames arriving from the camera vs. frames actually run through object detection. Healthy `camera_fps` with permanent 0.0 `detect_fps` = detection dead while streams live. |
| **`inference_speed`** | Frigate's rolling average of detector round-trip time, in ms. Sane values are hardware-dependent (ours: ~11 ms GPU over LAN); sub-3 ms means replies without work. |
| **async connect** | ZMQ `connect()` never fails fast; sends toward an absent peer queue optimistically and "succeed" locally. |
| **`Resource temporarily unavailable`** | EAGAIN surfacing through the plugin's send/recv timeouts while the peer is unreachable. |
| **detection watchdog** | Frigate's internal monitor that restarts the detection process when it looks stuck. Disarmed by the false-ready state (nothing looks stuck when empty replies return instantly). |
| **known-answer probe** | A synthetic end-to-end test with a known expected outcome — here, injecting a labeled clip into a test camera and asserting it produces a detection event. |
| **socket linger** | How long ZMQ keeps unsent messages after close (`linger_ms: 0` here = discard immediately). |
| **event drought** | An implausibly long span with zero object events on cameras whose streams are healthy — the cause-agnostic symptom of dead detection. |

## Repo layout

| Path | What |
|---|---|
| [`EVIDENCE.md`](EVIDENCE.md) | Incident 1 (2026-07-25) evidence bank: timeline, verbatim log captures (preserved before rotation destroyed them), host/container state, stats, probe ledger, recovery record, proven-vs-hypothesized ledger |
| [`EVIDENCE-INCIDENT-2.md`](EVIDENCE-INCIDENT-2.md) | Incident 2 (2026-07-27) evidence bank: same wedge via EAGAIN on a live re-init — no reboot involved; timeline, failure-state captures, recovery, proven-vs-undetermined ledger |
| [`tools/detect-liveness-check.py`](tools/detect-liveness-check.py) | Stdlib-only external liveness check (inference sanity band + event drought); cron it, wire the exit code to your alerting |
| `LICENSE` | MIT |

Internal RFC1918 addresses are consistently sanitized (`<NVR>`, `<SPARK>`);
every timestamp and log line is otherwise byte-accurate as captured.

## Contributing

Corroboration is the most valuable contribution: if you hit this failure
state (or reproduce it in a sandbox — hard-kill the detector peer during
the handshake window and watch for a ready-flip), please open an issue
here with your version, endpoint type (tcp/ipc), and the relevant log
window. Corrections to the analysis are equally welcome — especially
from anyone who can pin the exact queue mechanics behind the stale-reply
flip. Keep reports factual: logs and timestamps over theories.

## License

MIT — see [`LICENSE`](LICENSE). Use anything here (including the
liveness-check tool) freely; attribution appreciated.

---

*Found and documented 2026-07-25 during live incident response on a
production 13-camera deployment. Same environment as
[frigate-dgx-spark](https://github.com/tempered-steel/frigate-dgx-spark).
Second incident documented 2026-07-28 under the same process.*

*Disclosure: this dossier was compiled by an AI agent working under the
operator's direction during the live incident response. Every log line,
timestamp, and state capture is verbatim from the affected system; the
proven-vs-hypothesized split is maintained deliberately so readers can
weigh the evidence themselves.*
