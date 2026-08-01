#!/usr/bin/env python3
"""
detect-liveness-check.py -- external liveness check for Frigate object
detection. Catches the "silent zero detections" failure state this repo
documents (a detector plugin wedged while every health signal stays
green), from OUTSIDE Frigate, using only its HTTP API.

WHY AN EXTERNAL CHECK: in the documented incident, Frigate's own watchdog
was disarmed by a false "model ready" state -- the failure produced no
errors, no restarts, and no log lines for 15 hours. A monitor that lives
inside the monitored system inherits its blind spots. This script is the
generic version of the check that now guards our production plant; in a
backtest against the real incident it fires at the first run after the
wedge (~30 minutes in, vs. the 14+ hours a human took). Validated in live
use during a second, independent occurrence of the wedge (2026-07-27, see
EVIDENCE-INCIDENT-2.md): both signals returned CRIT.

TWO SIGNALS:

  1. INFERENCE SANITY (--floor-ms / --ceil-ms): a detector's reported
     inference_speed below the floor is physically implausible (a TCP
     round trip plus real inference cannot average 0.58 ms -- that number
     means junk replies) => CRIT. Above the ceiling is the documented
     silent-CPU-fallback signature (~20 ms healthy -> 80 ms+) => WARN.
     Healthy reference points from our hardware: ~11 ms (GB10 over LAN).

  2. VISUAL-EVENT DROUGHT (--drought-hours): hours since the newest
     object event across your detecting cameras, while camera streams
     are demonstrably up (camera_fps > 0). A dead detector produces
     exactly this: healthy streams, zero events.

     Quiet nights make a naive drought clock cry wolf (our 11-day event
     ledger showed overnight gaps up to 13.5 h are NORMAL). Two ways to
     handle that, pick one:
       a) leave --daylight-utc unset and keep --drought-hours high
          (default 18): safe everywhere, slower to fire; or
       b) set --daylight-utc to your active window (e.g. "12-1" = 12:00Z
          through 01:00Z, wrapping midnight) and drop --drought-hours to
          3-4: only daylight hours count toward the drought, so night
          silence never alarms. Our production setting. Site-specific:
          derive your own window from your event history before
          trusting a tight threshold.

     TIP: if you run a synthetic known-answer probe (inject a labeled
     clip into a test camera daily and assert it produces an event --
     strongly recommended, it is what caught our incident), include that
     camera in --cameras: its daily event resets the drought clock on
     quiet days and makes the check nearly false-positive-free.

EXIT CODES: 0 = all checks pass, 1 = warning only, 2 = critical.
Wire the exit code into whatever alerting you already trust (cron +
mail, healthchecks.io ping, Gotify/ntfy curl, Nagios/Zabbix).

EXAMPLES:
  # conservative defaults, autodetect cameras:
  detect-liveness-check.py --frigate http://nvr:5000

  # our production shape: daylight-weighted, tight threshold, probe cam:
  detect-liveness-check.py --frigate http://nvr:5000 \
      --daylight-utc 12-1 --drought-hours 3 \
      --cameras cam109,cam117,cam120,cam14,camtest

Stdlib only; no dependencies. Tested against Frigate 0.17.2.
"""
import argparse
import json
import sys
import time
import urllib.request


def api(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.load(r)


def detecting_cameras(base):
    """Cameras with object detection enabled."""
    cams = []
    for name, c in api(base, "/api/config")["cameras"].items():
        if c.get("detect", {}).get("enabled"):
            cams.append(name)
    return sorted(cams)


def in_window(hour, window):
    start, end = window
    return (start <= hour < end) if start < end else (hour >= start or hour < end)


def counted_seconds(t0, t1, window):
    """Seconds of (t0, t1] that count toward the drought (all, or window)."""
    if window is None:
        return max(0, t1 - t0)
    total, t = 0, t0
    while t < t1:
        step = min(300, t1 - t)
        if in_window(time.gmtime(t + step / 2).tm_hour, window):
            total += step
        t += step
    return total


def main():
    ap = argparse.ArgumentParser(
        description="External liveness check for Frigate object detection.")
    ap.add_argument("--frigate", required=True,
                    help="Frigate base URL, e.g. http://nvr:5000")
    ap.add_argument("--floor-ms", type=float, default=3.0,
                    help="inference_speed below this = junk replies -> CRIT")
    ap.add_argument("--ceil-ms", type=float, default=70.0,
                    help="inference_speed above this = CPU-fallback suspect -> WARN")
    ap.add_argument("--drought-hours", type=float, default=18.0,
                    help="counted hours without any object event -> CRIT")
    ap.add_argument("--daylight-utc", default=None, metavar="START-END",
                    help="UTC hour window that counts toward the drought, "
                         "e.g. 12-1 (wraps midnight). Unset = all hours count.")
    ap.add_argument("--cameras", default=None,
                    help="comma-separated cameras to watch (default: all "
                         "detect-enabled cameras)")
    ap.add_argument("--lookback-days", type=float, default=7.0,
                    help="how far back to search for the newest event")
    args = ap.parse_args()
    base = args.frigate.rstrip("/")
    window = None
    if args.daylight_utc:
        s, e = args.daylight_utc.split("-")
        window = (int(s), int(e))

    now = time.time()
    warns, crits = [], []

    # ---- Signal 1: inference sanity -------------------------------------
    try:
        stats = api(base, "/api/stats")
    except Exception as ex:
        print(f"CRIT frigate-api-unreachable: {ex}")
        sys.exit(2)
    for name, d in stats.get("detectors", {}).items():
        ms = d.get("inference_speed")
        if ms is None:
            continue
        if ms < args.floor_ms:
            crits.append(f"detector '{name}' inference_speed {ms}ms < "
                         f"{args.floor_ms}ms floor (junk-reply signature)")
        elif ms > args.ceil_ms:
            warns.append(f"detector '{name}' inference_speed {ms}ms > "
                         f"{args.ceil_ms}ms ceiling (CPU-fallback suspect)")

    # ---- Signal 2: event drought while streams are up --------------------
    cams = (args.cameras.split(",") if args.cameras
            else detecting_cameras(base))
    cam_stats = stats.get("cameras", {})
    streams_up = [c for c in cams
                  if (cam_stats.get(c, {}).get("camera_fps") or 0) > 0]
    if streams_up:
        newest = None
        for cam in cams:
            try:
                evs = api(base, f"/api/events?camera={cam}"
                                f"&after={int(now - args.lookback_days * 86400)}"
                                f"&before={int(now)}&limit=1")
            except Exception as ex:
                warns.append(f"event query failed for {cam}: {ex}")
                continue
            for e in evs:
                if newest is None or e["start_time"] > newest:
                    newest = e["start_time"]
        since = newest if newest else now - args.lookback_days * 86400
        counted_h = counted_seconds(since, now, window) / 3600.0
        if counted_h > args.drought_hours:
            last = (time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(since))
                    if newest else f"none in {args.lookback_days:g}d")
            crits.append(
                f"event drought: {counted_h:.1f} counted hours without any "
                f"object event on {len(cams)} camera(s) while "
                f"{len(streams_up)} stream(s) are up (last event: {last})")
    else:
        warns.append("no watched camera has camera_fps > 0 -- streams down "
                     "or wrong camera list; drought check skipped")

    # ---- Verdict ----------------------------------------------------------
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    for c in crits:
        print(f"{stamp}  CRIT {c}")
    for w in warns:
        print(f"{stamp}  WARN {w}")
    if not crits and not warns:
        det = ", ".join(f"{n}={d.get('inference_speed')}ms"
                        for n, d in stats.get("detectors", {}).items())
        print(f"{stamp}  PASS detection alive ({det}; "
              f"{len(streams_up)}/{len(cams)} streams up)")
    sys.exit(2 if crits else (1 if warns else 0))


if __name__ == "__main__":
    main()
