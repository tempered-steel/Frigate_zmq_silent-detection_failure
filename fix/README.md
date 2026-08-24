# Unofficial fix — Frigate zmq detector silent wedge

This directory contains a working, tested fix for the failure modes
documented in this repository (and reported upstream as
[discussion #23883](https://github.com/blakeblackshear/frigate/discussions/23883)):
the zmq detector plugin never retries a failed model init/transfer, so one
failure — detector peer unavailable, or an EAGAIN timeout during a live
re-init — leaves `detect_raw()` silently serving zero detections until
Frigate is restarted, while all health signals stay green. A stale buffered
reply can also satisfy the ready handshake against a peer that is no longer
alive. See `../EVIDENCE.md` (incident 1) and `../EVIDENCE-INCIDENT-2.md`
(incident 2) for the full live-incident evidence.

**Status: UNOFFICIAL and UNMERGED.** Current as of 2026-08-24. Future
Frigate releases may drift from these files. No maintenance is promised.
Use at your own risk, as-is.

## What the fix changes (one file: `frigate/detectors/plugins/zmq_ipc.py`)

1. **Self-recovery with backoff.** Failed initialization is retried —
   every not-ready `detect_raw()` call and every request failure may
   re-attempt init, rate-limited by a new `reinit_backoff_ms` detector
   config option (default 5000 ms). A dead peer is polled, not hammered;
   a returning peer is picked up automatically without a restart.
2. **Stale-reply immunity.** `REQ_CORRELATE` + `REQ_RELAXED` on the REQ
   socket: libzmq drops replies that do not correlate with the current
   outstanding request. Works against an unmodified REP peer.
3. **Confirmed ready handshake.** Handshake runs on a fresh socket and
   requires two consecutive successful exchanges before arming
   `_model_ready`.
4. **Log hygiene.** The not-ready warning is rate-limited to one line per
   60 s with a suppressed-line count, replacing a per-frame flood.

Healthy-path behavior is unchanged; the only config addition is the
optional `reinit_backoff_ms` key.

## Files

| File | What it is |
|---|---|
| `zmq_ipc-v0.17.2-patched.py` | Drop-in replacement for Frigate **v0.17.2** (current stable line) |
| `zmq_ipc-dev-patched.py` | Same fix rebased onto the **dev** branch as of 2026-08-24 |
| `zmq_ipc_fix-v0.17.2.patch` | Unified diff against v0.17.2 |
| `zmq_ipc_fix-dev.patch` | Unified diff against dev (2026-08-24) |
| `repro.py` | Standalone repro + regression harness — scripted REP peer over loopback TCP; needs only `pyzmq`, `numpy`, `pydantic`. Reproduces both failure modes on the UNPATCHED plugin and verifies the fix (12 checks). No Frigate installation required. |
| `repro-output-v0.17.2-20260814.txt` | Banked 12/12 passing run against v0.17.2 |
| `repro-output-dev-20260824.txt` | Banked 12/12 passing run against dev (2026-08-24) |

## Installation (Docker, no image rebuild)

Bind-mount the patched file over the plugin inside the container. In your
compose service for Frigate:

```yaml
    volumes:
      # ... your existing volumes ...
      - /path/to/zmq_ipc-v0.17.2-patched.py:/opt/frigate/frigate/detectors/plugins/zmq_ipc.py:ro
```

Then recreate the container. Verify the override took by checking the file
inside the container:

```
docker exec frigate sha256sum /opt/frigate/frigate/detectors/plugins/zmq_ipc.py
```

Expected (v0.17.2 variant):
`04866933cf427cb3e355d0702c035f42ceb7f04252a2c348b06591e530b0ffbf`

Optional detector config addition (defaults shown):

```yaml
detectors:
  zmq:
    type: zmq
    endpoint: tcp://<your-detector-host>:<port>
    reinit_backoff_ms: 5000
```

## Running the harness yourself

```
python3 -m venv venv && venv/bin/pip install pyzmq numpy pydantic
# place repro.py next to directories upstream/ and patched/ each containing
# a zmq_ipc.py (see the path constants at the top of repro.py)
venv/bin/python3 repro.py
```

Expected: 12 checks, 0 failed — the four "upstream" checks REPRODUCE the
defects on the unpatched file; the eight "patched" checks prove the fixes
and the unchanged healthy path.

## Upstream status

A pull request carrying this fix was prepared from the fork branch
[`tempered-steel/frigate:fix/zmq-detector-silent-wedge`](https://github.com/tempered-steel/frigate/tree/fix/zmq-detector-silent-wedge).
If it is ever merged upstream, prefer the official release over these files.

## License and attribution

The patched plugin files are derivative of Frigate
(https://github.com/blakeblackshear/frigate), Copyright (c) 2019 Blake
Blackshear, MIT License. The modifications, the harness, and this
documentation are Copyright (c) 2026 Tempered-Steel, MIT License (see
`../LICENSE`). Both notices travel with the files.
