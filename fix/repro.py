#!/usr/bin/env python3
"""Sandbox repro + regression harness for the Frigate zmq detector wedge.

Runs the UPSTREAM v0.17.2 plugin and the PATCHED plugin against a scripted
REP peer and demonstrates, per scenario, the defect on upstream and the fix
on the patch. Pure loopback TCP, in-process server threads, no Frigate, no
plant contact. Stubs stand in for frigate.detectors.{detection_api,
detector_config}.

Scenarios (mapped to the filed evidence, discussion #23883):
  S1  baseline        healthy peer -> both plugins init ready and return real
                      detections (also proves REQ_CORRELATE is compatible
                      with an unmodified REP peer).
  S2  wedge/recovery  peer present but init fails once (refuses the model) ->
                      peer becomes healthy. Upstream: _model_ready stays
                      False forever (incident-2 class: one failed init =
                      zeros until restart). Patched: recovers by itself
                      after the backoff.
  S3  false-ready     peer answers the FIRST ready exchange then goes silent
                      (proxy for one stale/spurious reply, incident-1 class).
                      Upstream: arms itself on the single reply. Patched:
                      double-handshake refuses to arm.
  S4  log flood       not-ready path called rapidly. Upstream: one warning
                      per call (the spam that destroyed ~200x of log history
                      in incident 1). Patched: 1 line + suppressed counter.
  S5  watchdog budget healthy peer goes SILENT mid-run (TCP up, no replies
                      -- the 2026-08-30/31 UDM-restart class). One detect_raw
                      call then runs the inline re-init. Upstream: blocks for
                      the 30 s model-operation timeout, far past Frigate's
                      10 s detector watchdog (which SIGKILLs the process and
                      strands the detection-queue lock -- captured 2026-08-31).
                      Patched v1.3: bounded by recovery_timeout_ms, then the
                      backoff makes later calls instant, and the plugin
                      recovers by itself when the peer answers again.

Usage: run with a python that has pyzmq + numpy + pydantic:
  <venv>/bin/python repro.py
Exit code 0 = every assertion passed.
"""

import importlib.util
import json
import logging
import os
import sys
import tempfile
import threading
import time
import types

import zmq

BASE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM = os.path.join(BASE, "..", "upstream", "zmq_ipc.py")
PATCHED = os.path.join(BASE, "..", "patched", "zmq_ipc.py")

# --------------------------------------------------------------------------
# Stubs for the two frigate imports the plugin makes. Everything else the
# plugin touches (zmq, numpy, pydantic) is real.
# --------------------------------------------------------------------------
import pydantic


class DetectionApi:
    def __init__(self, detector_config):
        self.detector_config = detector_config


class BaseDetectorConfig(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        arbitrary_types_allowed=True, protected_namespaces=()
    )
    model: object = None


def _install_stubs():
    pkg = types.ModuleType("frigate")
    det = types.ModuleType("frigate.detectors")
    da = types.ModuleType("frigate.detectors.detection_api")
    dc = types.ModuleType("frigate.detectors.detector_config")
    da.DetectionApi = DetectionApi
    dc.BaseDetectorConfig = BaseDetectorConfig
    pkg.detectors = det
    det.detection_api = da
    det.detector_config = dc
    sys.modules.update(
        {
            "frigate": pkg,
            "frigate.detectors": det,
            "frigate.detectors.detection_api": da,
            "frigate.detectors.detector_config": dc,
        }
    )


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ModelType:
    name = "ssd"


class _Model:
    def __init__(self, path):
        self.path = path
        self.model_type = _ModelType()


# --------------------------------------------------------------------------
# Scripted REP peer. Modes:
#   healthy       answer everything correctly (marker detections)
#   refuse        model_request -> available False; model_data -> saved False
#   one_then_dead answer the first model_request ready-True, then stop
# Mode is switchable live via set_mode(). S5 uses BlackholePeer below.
# --------------------------------------------------------------------------
import numpy as np

MARKER = np.zeros((20, 6), np.float32)
MARKER[0] = [1.0, 0.9, 0.1, 0.1, 0.2, 0.2]  # class 1 @ 0.9 -> "real" result


class Peer(threading.Thread):
    def __init__(self, endpoint):
        super().__init__(daemon=True)
        self.endpoint = endpoint
        self.mode = "healthy"
        self.answered = 0
        self._stop = threading.Event()

    def set_mode(self, mode):
        self.mode = mode

    def stop(self):
        self._stop.set()

    def run(self):
        ctx = zmq.Context()
        sock = ctx.socket(zmq.REP)
        sock.setsockopt(zmq.RCVTIMEO, 100)
        sock.bind(self.endpoint)
        while not self._stop.is_set():
            try:
                frames = sock.recv_multipart()
            except zmq.Again:
                continue
            self.answered += 1
            reply = self._reply_for(frames)
            if reply is None:
                # deliberately silent: a dead peer stays dead for the rest
                # of its scenario, so stop serving instead of resetting
                break
            sock.send_multipart(reply)
        sock.close(linger=0)
        ctx.term()

    def _reply_for(self, frames):
        header = json.loads(frames[0].decode("utf-8"))
        if header.get("model_request"):
            if self.mode == "healthy":
                return [json.dumps(
                    {"model_available": True, "model_loaded": True}
                ).encode()]
            if self.mode == "refuse":
                return [json.dumps(
                    {"model_available": False, "model_loaded": False}
                ).encode()]
            if self.mode == "one_then_dead":
                self.mode = "dead"
                return [json.dumps(
                    {"model_available": True, "model_loaded": True}
                ).encode()]
            return None  # dead
        if header.get("model_data"):
            if self.mode == "healthy":
                return [json.dumps(
                    {"model_saved": True, "model_loaded": True}
                ).encode()]
            if self.mode == "refuse":
                return [json.dumps(
                    {"model_saved": False, "model_loaded": False}
                ).encode()]
            return None
        # inference request
        if self.mode == "healthy":
            return [MARKER.tobytes()]
        return None


class BlackholePeer(Peer):
    """Half-open peer for S5: a ROUTER socket (no REQ/REP lockstep) that stays
    bound and connected the whole time. Mode "healthy" answers like Peer;
    mode "blackhole" CONSUMES every request and never answers -- TCP is up,
    the request is gone, the client only ever sees its own timeout. This is
    the shape of a peer behind a rebooting router as seen from the client,
    and it cannot die of the REP state machine the way Peer would.
    The envelope (identity .. empty delimiter, incl. the REQ_CORRELATE id
    frame when present) is echoed back verbatim, exactly as a REP does."""

    def __init__(self, endpoint):
        super().__init__(endpoint)
        self.dropped = 0

    def run(self):
        ctx = zmq.Context()
        sock = ctx.socket(zmq.ROUTER)
        sock.setsockopt(zmq.RCVTIMEO, 100)
        sock.bind(self.endpoint)
        while not self._stop.is_set():
            try:
                frames = sock.recv_multipart()
            except zmq.Again:
                continue
            if self.mode == "blackhole":
                self.dropped += 1
                continue
            try:
                cut = frames.index(b"") + 1
            except ValueError:
                self.dropped += 1
                continue
            envelope, body = frames[:cut], frames[cut:]
            self.answered += 1
            reply = self._reply_for(body)
            if reply is None:
                self.dropped += 1
                continue
            sock.send_multipart(envelope + reply)
        sock.close(linger=0)
        ctx.term()


def make_detector(mod, endpoint, extra_cfg=None):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".model") as f:
        f.write(b"\x00" * 1024)
        model_path = f.name
    cfg_fields = dict(
        type="zmq",
        endpoint=endpoint,
        request_timeout_ms=200,
        linger_ms=0,
        model=_Model(model_path),
    )
    if extra_cfg:
        cfg_fields.update(extra_cfg)
    cfg = mod.ZmqDetectorConfig(**cfg_fields)
    return mod.ZmqIpcDetector(cfg)


FRAME = np.zeros((1, 300, 300, 3), np.uint8)
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


PORT = 47311


def fresh_peer(mode, cls=Peer):
    global PORT
    PORT += 1
    ep = f"tcp://127.0.0.1:{PORT}"
    p = cls(ep)
    p.mode = mode
    p.start()
    time.sleep(0.05)
    return p, ep


def s1_baseline(mod, label):
    print(f"S1 baseline healthy peer [{label}]")
    peer, ep = fresh_peer("healthy")
    det = make_detector(mod, ep)
    check(f"{label}: ready after init", det._model_ready)
    out = det.detect_raw(FRAME)
    check(f"{label}: real detections returned", float(out[0][1]) == np.float32(0.9),
          f"row0={out[0][:2]}")
    peer.stop()


def s2_wedge_recovery(mod, label, expect_recovery):
    print(f"S2 failed-init wedge, peer then healthy [{label}]")
    peer, ep = fresh_peer("refuse")
    det = make_detector(mod, ep, extra_cfg=(
        {"reinit_backoff_ms": 300} if expect_recovery else None))
    check(f"{label}: init failed as scripted", not det._model_ready)
    peer.set_mode("healthy")  # the peer is now fully functional
    deadline = time.monotonic() + 3.0
    recovered = False
    while time.monotonic() < deadline:
        out = det.detect_raw(FRAME)
        if float(out[0][1]) == np.float32(0.9):
            recovered = True
            break
        time.sleep(0.05)
    if expect_recovery:
        check(f"{label}: recovered without restart", recovered)
    else:
        check(f"{label}: DEFECT REPRODUCED -- still wedged 3s after peer healthy",
              not recovered and not det._model_ready)
    peer.stop()


def s3_false_ready(mod, label, expect_armed):
    print(f"S3 single spurious ready reply [{label}]")
    peer, ep = fresh_peer("one_then_dead")
    det = make_detector(mod, ep)
    if expect_armed:
        check(f"{label}: DEFECT REPRODUCED -- armed by ONE unconfirmed reply",
              det._model_ready)
    else:
        check(f"{label}: double-handshake refused to arm", not det._model_ready)
    peer.stop()


def s4_log_flood(mod, label, expect_flood):
    print(f"S4 not-ready log volume over 50 rapid calls [{label}]")
    peer, ep = fresh_peer("refuse")
    det = make_detector(mod, ep, extra_cfg=(
        None if expect_flood else {"reinit_backoff_ms": 60000}))
    records = []

    class Trap(logging.Handler):
        def emit(self, r):
            if "not ready" in r.getMessage().lower():
                records.append(r.getMessage())

    trap = Trap()
    mod_logger = logging.getLogger(mod.__name__)
    prev_level, prev_prop = mod_logger.level, mod_logger.propagate
    mod_logger.setLevel(logging.WARNING)  # global CRITICAL gate would eat the records
    mod_logger.propagate = False
    mod_logger.addHandler(trap)
    for _ in range(50):
        det.detect_raw(FRAME)
    mod_logger.removeHandler(trap)
    mod_logger.setLevel(prev_level)
    mod_logger.propagate = prev_prop
    if expect_flood:
        check(f"{label}: DEFECT REPRODUCED -- {len(records)} warnings for 50 calls",
              len(records) == 50)
    else:
        check(f"{label}: rate-limited to {len(records)} warning(s)", len(records) <= 1)
    peer.stop()


def s5_watchdog_budget(mod, label, expect_bounded):
    print(f"S5 half-open peer mid-run: one detect_raw wall time vs 10 s watchdog [{label}]")
    # Test knobs: backoff 3000 ms (> the 2.2 s attempt, as production's 5000 ms
    # is) so a call right after an attempt is provably inside the backoff; the
    # outage begins OUTSIDE the post-init window after a 3.1 s settle.
    # Production: 5000 ms backoff, 2000 ms budget -> one 2.2 s attempt per 5 s
    # while a peer is unreachable, every other call instant zeros.
    peer, ep = fresh_peer("healthy", cls=BlackholePeer)
    det = make_detector(mod, ep, extra_cfg=(
        {"reinit_backoff_ms": 3000, "recovery_timeout_ms": 2000} if expect_bounded else None))
    check(f"{label}: ready before the outage", det._model_ready)
    time.sleep(3.1)
    peer.set_mode("blackhole")
    t0 = time.monotonic()
    det.detect_raw(FRAME)          # request times out -> inline re-init runs
    dt = time.monotonic() - t0
    if expect_bounded:
        check(f"{label}: detect_raw bounded to {dt:.2f}s (< 10 s watchdog; budget 2.0 s + 0.2 s request)",
              2.0 <= dt < 5.0)
        t1 = time.monotonic()
        det.detect_raw(FRAME)      # inside the backoff: no attempt, instant zeros
        dt2 = time.monotonic() - t1
        check(f"{label}: next call inside backoff returned in {dt2*1000:.0f} ms", dt2 < 0.1)
        peer.set_mode("healthy")   # peer answers again
        deadline = time.monotonic() + 8.0
        recovered = False
        t2 = time.monotonic()
        while time.monotonic() < deadline:
            out = det.detect_raw(FRAME)
            if float(out[0][1]) == np.float32(0.9):
                recovered = True
                break
            time.sleep(0.05)
        check(f"{label}: recovered by itself {time.monotonic()-t2:.1f}s after the peer returned (dropped {peer.dropped} requests during the outage)",
              recovered)
    else:
        check(f"{label}: DEFECT REPRODUCED -- detect_raw blocked {dt:.1f}s (> 10 s watchdog)", dt > 10.0)
    peer.stop()


def main():
    logging.basicConfig(level=logging.CRITICAL)  # silence plugin noise; S4 traps directly
    _install_stubs()
    orig = _load(UPSTREAM, "zmq_ipc_upstream")
    patched = _load(PATCHED, "zmq_ipc_patched")

    print("=== upstream v0.17.2 (defect demonstration) ===")
    s1_baseline(orig, "upstream")
    s2_wedge_recovery(orig, "upstream", expect_recovery=False)
    s3_false_ready(orig, "upstream", expect_armed=True)
    s4_log_flood(orig, "upstream", expect_flood=True)
    s5_watchdog_budget(orig, "upstream", expect_bounded=False)

    print("=== patched (fix verification) ===")
    s1_baseline(patched, "patched")
    s2_wedge_recovery(patched, "patched", expect_recovery=True)
    s3_false_ready(patched, "patched", expect_armed=False)
    s4_log_flood(patched, "patched", expect_flood=False)
    s5_watchdog_budget(patched, "patched", expect_bounded=True)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results)} checks, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
