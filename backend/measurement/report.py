"""Slice 1 single-page session report generator.

Reads a session bundle (created by ``measurement.run_session``) and
emits a single-page markdown report with the metrics that the
architecture proposal Section 9 implications care about:

- Session overview (name, duration, wall-clock window)
- Per-speaker RSSI median + variance + min + max
- xrun count per node
- Route activity (creates / teardowns) during the window
- BlueZ MediaTransport snapshots (codec / bitpool / volume / delay)
- RSSI-vs-xrun temporal correlation
  (does dropout cluster within N seconds of an RSSI dip?)
- Mic capture summary
- Subjective notes slot for the human to fill in

Pure stdlib. Numbers are computed with the statistics module; the
RSSI-vs-xrun correlation is a simple "fraction of xruns whose nearest
preceding rssi_sample was at least DIP_THRESHOLD dB below the
60s baseline" because we do not have scipy and a Pearson r between
1 Hz RSSI and event-driven xruns is not meaningful anyway.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add backend/ to sys.path so we can import syncsonic_ble.* when invoked
# directly via "python -m measurement.report" from /backend.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from syncsonic_ble.telemetry import telemetry_root  # noqa: E402

# How many seconds before an xrun should we look for a "dip"?
DIP_LOOKBACK_SEC = 5.0
# How much below the 60s baseline qualifies as a dip (dB)?
DIP_THRESHOLD_DB = 5.0


def _parse_wall_iso(s: str) -> Optional[float]:
    """Parse our 'YYYY-MM-DDTHH:MM:SS.mmmZ' to a unix epoch float."""
    if not s:
        return None
    try:
        # datetime.fromisoformat handles +00:00 but not Z in 3.10
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).timestamp()
    except (TypeError, ValueError):
        return None


def _load_events(events_path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not events_path.exists():
        return out
    with open(events_path, "r", encoding="ascii") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            obj["_wall_unix"] = _parse_wall_iso(obj.get("wall_iso", ""))
            out.append(obj)
    return out


def _load_session_meta(bundle_dir: Path) -> Dict[str, Any]:
    meta_path = bundle_dir / "session.json"
    if not meta_path.exists():
        return {}
    with open(meta_path, "r", encoding="ascii") as fh:
        return json.load(fh)


def _rssi_summary(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_mac: Dict[str, List[int]] = defaultdict(list)
    hci_for_mac: Dict[str, str] = {}
    for ev in events:
        if ev.get("event_type") != "rssi_sample":
            continue
        d = ev.get("data") or {}
        mac = d.get("mac")
        rssi = d.get("rssi_dbm")
        if not mac or rssi is None:
            continue
        by_mac[mac].append(int(rssi))
        if d.get("hci"):
            hci_for_mac[mac] = d["hci"]
    out: Dict[str, Dict[str, Any]] = {}
    for mac, samples in by_mac.items():
        if not samples:
            continue
        out[mac] = {
            "hci": hci_for_mac.get(mac, ""),
            "n_samples": len(samples),
            "median_dbm": statistics.median(samples),
            "min_dbm": min(samples),
            "max_dbm": max(samples),
            "stdev_db": round(statistics.pstdev(samples), 2) if len(samples) > 1 else 0.0,
        }
    return out


def _xrun_counts(events: List[Dict[str, Any]]) -> Counter:
    c: Counter = Counter()
    for ev in events:
        if ev.get("event_type") != "pw_xrun":
            continue
        d = ev.get("data") or {}
        node = d.get("node_name", "") or f"<id-{d.get('node_id', '?')}>"
        c[node] += 1
    return c


def _route_activity(events: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "route_create": sum(1 for e in events if e.get("event_type") == "route_create"),
        "route_teardown": sum(1 for e in events if e.get("event_type") == "route_teardown"),
        "set_latency_request": sum(1 for e in events if e.get("event_type") == "set_latency_request"),
        "set_volume_request": sum(1 for e in events if e.get("event_type") == "set_volume_request"),
        "bluez_connect": sum(1 for e in events if e.get("event_type") == "bluez_connect"),
        "bluez_disconnect": sum(1 for e in events if e.get("event_type") == "bluez_disconnect"),
    }


def _bluez_transport_summary(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Latest snapshot per device path."""
    latest: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        if ev.get("event_type") != "bluez_transport_snapshot":
            continue
        for t in (ev.get("data") or {}).get("transports", []):
            path = t.get("path", "")
            if not path:
                continue
            latest[path] = t
    return list(latest.values())


def _decode_sbc_config(cfg_bytes: List[int]) -> Dict[str, Any]:
    """Decode the SBC Configuration bytes into human-readable fields."""
    if not cfg_bytes or len(cfg_bytes) < 4:
        return {"raw": cfg_bytes}
    b0, b1 = cfg_bytes[0], cfg_bytes[1]
    freq_map = {0x80: 16000, 0x40: 32000, 0x20: 44100, 0x10: 48000}
    chmode_map = {0x08: "mono", 0x04: "dual", 0x02: "stereo", 0x01: "joint_stereo"}
    blk_map = {0x80: 4, 0x40: 8, 0x20: 12, 0x10: 16}
    sub_map = {0x08: 4, 0x04: 8}
    alloc_map = {0x02: "snr", 0x01: "loudness"}
    return {
        "sample_rate_hz": freq_map.get(b0 & 0xF0),
        "channel_mode": chmode_map.get(b0 & 0x0F),
        "block_length": blk_map.get(b1 & 0xF0),
        "subbands": sub_map.get(b1 & 0x0C),
        "allocation": alloc_map.get(b1 & 0x03),
        "min_bitpool": cfg_bytes[2] if len(cfg_bytes) > 2 else None,
        "max_bitpool": cfg_bytes[3] if len(cfg_bytes) > 3 else None,
    }


def _rssi_vs_xrun_correlation(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """For each xrun, find the most recent rssi_baseline AND the most
    recent rssi_sample within DIP_LOOKBACK_SEC, and check whether the
    sample was DIP_THRESHOLD_DB below the baseline. Counts are per-mac."""
    # Build sorted timeseries
    rssi_baseline: Dict[str, List] = defaultdict(list)
    rssi_sample: Dict[str, List] = defaultdict(list)
    xruns: List = []
    for ev in events:
        wall = ev.get("_wall_unix")
        if wall is None:
            continue
        et = ev.get("event_type")
        d = ev.get("data") or {}
        if et == "rssi_baseline":
            mac = d.get("mac")
            if mac and d.get("median_60s") is not None:
                rssi_baseline[mac].append((wall, float(d["median_60s"])))
        elif et == "rssi_sample":
            mac = d.get("mac")
            if mac and d.get("rssi_dbm") is not None:
                rssi_sample[mac].append((wall, int(d["rssi_dbm"])))
        elif et == "pw_xrun":
            xruns.append((wall, d.get("node_name", "")))

    n_xruns = len(xruns)
    n_dip_correlated = 0
    n_no_rssi_data = 0
    by_node: Counter = Counter()
    for ts, node_name in xruns:
        # Try to attribute to a mac via the bluez_output node name.
        target_mac = None
        for mac in rssi_sample.keys():
            mac_token = mac.replace(":", "_")
            if mac_token in (node_name or ""):
                target_mac = mac
                break
        if target_mac is None:
            n_no_rssi_data += 1
            continue
        # Find latest baseline at or before ts
        baselines = [(w, v) for w, v in rssi_baseline.get(target_mac, []) if w <= ts]
        samples = [(w, v) for w, v in rssi_sample.get(target_mac, []) if ts - DIP_LOOKBACK_SEC <= w <= ts]
        if not baselines or not samples:
            n_no_rssi_data += 1
            continue
        baseline_v = baselines[-1][1]
        worst_sample = min(s for _, s in samples)
        if (baseline_v - worst_sample) >= DIP_THRESHOLD_DB:
            n_dip_correlated += 1
            by_node[node_name] += 1
    return {
        "n_xruns": n_xruns,
        "n_dip_correlated": n_dip_correlated,
        "n_no_rssi_data": n_no_rssi_data,
        "fraction_correlated": (n_dip_correlated / n_xruns) if n_xruns else 0.0,
        "by_node": dict(by_node),
        "dip_threshold_db": DIP_THRESHOLD_DB,
        "lookback_sec": DIP_LOOKBACK_SEC,
    }


def _format_md(meta: Dict[str, Any], events: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    out.append(f"# Session report: {meta.get('name', '<unknown>')}")
    out.append("")
    out.append(f"- **duration**: {meta.get('duration_sec')}s")
    out.append(f"- **start (UTC)**: {meta.get('start_wall_iso')}")
    out.append(f"- **end (UTC)**: {meta.get('end_wall_iso')}")
    out.append(f"- **events kept**: {meta.get('events_kept', '?')}")
    out.append(f"- **bundle**: `{meta.get('bundle_dir', '?')}`")
    out.append("")

    out.append("## Per-speaker RSSI")
    out.append("")
    rssi = _rssi_summary(events)
    if not rssi:
        out.append("_No rssi_sample events in this window. Either no speakers were connected, or the RSSI sampler failed._")
    else:
        out.append("| MAC | hci | samples | median dBm | min | max | stdev dB |")
        out.append("|---|---|---:|---:|---:|---:|---:|")
        for mac, s in sorted(rssi.items()):
            out.append(
                f"| `{mac}` | {s['hci']} | {s['n_samples']} | {s['median_dbm']} | "
                f"{s['min_dbm']} | {s['max_dbm']} | {s['stdev_db']} |"
            )
    out.append("")

    out.append("## xrun count per node")
    out.append("")
    xc = _xrun_counts(events)
    if not xc:
        out.append("_No xrun events. Either nothing audible dropped, or no audio was playing._")
    else:
        out.append("| node | xruns |")
        out.append("|---|---:|")
        for node, n in xc.most_common():
            out.append(f"| `{node}` | {n} |")
    out.append("")

    out.append("## RSSI-vs-xrun correlation")
    out.append("")
    corr = _rssi_vs_xrun_correlation(events)
    out.append(
        f"Of **{corr['n_xruns']}** xruns, **{corr['n_dip_correlated']}** "
        f"({corr['fraction_correlated']:.0%}) were preceded within "
        f"{corr['lookback_sec']:.0f}s by an RSSI dip of "
        f">= {corr['dip_threshold_db']:.1f} dB below the 60s baseline. "
        f"{corr['n_no_rssi_data']} xruns had insufficient RSSI data to attribute."
    )
    if corr.get("by_node"):
        out.append("")
        out.append("Dip-correlated xruns by node:")
        for node, n in sorted(corr["by_node"].items(), key=lambda kv: -kv[1]):
            out.append(f"- `{node}`: {n}")
    out.append("")

    out.append("## Route + control activity")
    out.append("")
    ra = _route_activity(events)
    out.append("| event type | count |")
    out.append("|---|---:|")
    for k, v in ra.items():
        out.append(f"| `{k}` | {v} |")
    out.append("")

    out.append("## BlueZ MediaTransport snapshots (latest per device)")
    out.append("")
    bts = _bluez_transport_summary(events)
    if not bts:
        out.append("_No BlueZ transport snapshots in this window._")
    else:
        out.append("| MAC | state | codec | volume | delay | SBC: rate / mode / blk / sub / alloc / bitpool min-max |")
        out.append("|---|---|---:|---:|---:|---|")
        for t in bts:
            sbc = _decode_sbc_config(t.get("configuration_bytes", []))
            sbc_str = (
                f"{sbc.get('sample_rate_hz')} / {sbc.get('channel_mode')} / "
                f"{sbc.get('block_length')} blk / {sbc.get('subbands')} sub / "
                f"{sbc.get('allocation')} / {sbc.get('min_bitpool')}-{sbc.get('max_bitpool')}"
            )
            out.append(
                f"| `{t.get('mac', '')}` | {t.get('state', '')} | {t.get('codec', '')} | "
                f"{t.get('volume', '')} | {t.get('delay', '')} | {sbc_str} |"
            )
    out.append("")

    out.append("## Mic capture")
    out.append("")
    mic = (meta.get("mic_snapshot") or {}).get("files") or []
    if not mic:
        out.append("_No mic segments captured. The mic_capture process may not have started or the source was missing._")
    else:
        total_bytes = sum(int(f.get("size_bytes", 0)) for f in mic)
        out.append(f"- **segments**: {len(mic)}")
        out.append(f"- **total bytes**: {total_bytes}")
        # 48 kHz mono s16le -> 96000 bytes/sec
        out.append(f"- **approx audio duration**: {total_bytes / 96000:.1f}s")
        out.append("")
        out.append("| segment | size_bytes | mtime_unix |")
        out.append("|---|---:|---|")
        for f in mic:
            out.append(f"| `{f.get('name')}` | {f.get('size_bytes', 0)} | {f.get('mtime_unix', '')} |")
    out.append("")

    out.append("## Subjective notes")
    out.append("")
    out.append("_Fill in by hand after listening:_")
    out.append("")
    out.append("- Did you hear any dropouts? (yes/no, which speaker)")
    out.append("- Was inter-speaker drift perceptible?")
    out.append("- Anything else worth recording?")
    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Slice 1 session report")
    parser.add_argument("--bundle", default=None, help="Path to session bundle dir (default: sessions/latest)")
    parser.add_argument("--out", default=None, help="Where to write report.md (default: <bundle>/report.md)")
    args = parser.parse_args()

    if args.bundle:
        bundle_dir = Path(args.bundle)
    else:
        bundle_dir = (telemetry_root() / "sessions" / "latest").resolve()
    if not bundle_dir.exists() or not bundle_dir.is_dir():
        print(f"[report] bundle not found: {bundle_dir}", file=sys.stderr)
        return 1

    meta = _load_session_meta(bundle_dir)
    events = _load_events(bundle_dir / "events.jsonl")

    md = _format_md(meta, events)

    out_path = Path(args.out) if args.out else (bundle_dir / "report.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"[report] wrote {out_path} ({len(md)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
