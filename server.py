"""Grill Live: a local, read-only live dashboard for Pit Boss Wi-Fi grills.

Talks to the grill's own Wi-Fi module over your home network (Mongoose OS RPC on
port 80). It only reads: PB.GetState, Sys.GetInfo and an occasional Wi-Fi scan for
signal strength. It never sends commands to the grill's controller.

    python server.py                    # find the grill, then scan the QR code with your phone
    python server.py --grill 192.168.1.50
    python server.py --local-only       # only this computer can open the dashboard
    python server.py --demo             # simulated cook, no grill needed
"""

import argparse
import collections
import concurrent.futures
import csv
import json
import math
import os
import random
import socket
import statistics
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import qr

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
SETTINGS_PATH = os.path.join(HERE, "settings.json")
NO_PROBE = 960
FIELDS = ["t", "on", "grill", "set", "p1", "p2", "p3", "p4", "smoker", "p1Target", "p2Target",
          "fan", "auger", "igniter", "light", "prime", "errors"]
ERRORS = ["Probe 1 error", "Probe 2 error", "Probe 3 error", "Over temperature", "Fan error",
          "Igniter error", "Auger error", "Out of pellets", "Start-up failed (ErL)"]


def layout(temps, fahrenheit, on, n_errors, fan, igniter, auger, light, prime=None):
    status = {"on": on, "fan": fan, "igniter": igniter, "auger": auger, "light": light}
    if prime is not None:
        status["prime"] = prime
    return {"temps": temps, "fahrenheit": fahrenheit, "status": status,
            "errors": {on + 1 + i: ERRORS[i] for i in range(n_errors)},
            "tlen": max(max(temps.values()) + 3, fahrenheit + 1),
            "slen": max(list(status.values()) + [on + n_errors]) + 1}


# Byte offsets of the FE0C (temperatures) and FE0B (status) frames per control board,
# taken from the board definitions in pytboss (github.com/dknowles2/pytboss).
# Temperatures are three bytes of decimal digits; 960 means "no probe".
_A = layout({"p1Target": 2, "p2Target": 5, "p1": 8, "p2": 11, "p3": 14, "p4": 17, "smoker": 20,
             "set": 23, "grill": 26}, 29, 27, 9, 37, 38, 39, 40, 41)
_B_TEMPS = {"p1": 5, "p2": 8, "p3": 11, "p4": 14, "smoker": 17, "set": 20, "grill": 23}
_B = layout(_B_TEMPS, 26, 24, 9, 34, 35, 36, 37, 38)
_B_TARGET = layout(dict(_B_TEMPS, p1Target=2), 26, 24, 9, 34, 35, 36, 37, 38)
_L = layout({"p1": 5, "p2": 8, "p3": 11, "p4": 14, "set": 17, "grill": 20}, 23, 21, 9, 31, 32, 33, 34, 35)
_L_TARGET = layout({"p1Target": 2, "p1": 5, "p2": 8, "p3": 11, "p4": 14, "set": 17, "grill": 20},
                   23, 21, 9, 31, 32, 33, 34, 35)
_M = layout({"p1": 5, "p2": 8, "p3": 11, "set": 14, "grill": 17}, 20, 18, 8, 27, 28, 29, 30)
_VA = layout({"p1": 5, "p2": 8, "p3": 11, "set": 14, "grill": 17}, 20, 24, 9, 34, 35, 36, 37, 38)
BOARDS = {"PBA": _A, "PBB": _A, "PBD": _A, "PBE": _A, "PBT": _A,
          "PBC": _B, "PBC2": _B, "PBG": _B, "PBV": _B, "PBV2": _B,
          "PBL": _B_TARGET, "PBL2": _B_TARGET, "PBL3": _B_TARGET, "PBP": _B_TARGET,
          "LBL": _L_TARGET, "LFS": _L, "PBM": _M, "PBM2": _M, "PBVA": _VA}
TESTED_BOARDS = {"PBA"}

lock = threading.Lock()
samples = []
latest = {"sample": None, "info": None, "error": None, "fahrenheit": True, "lastOk": 0, "raw": None}
link = {"ok": 0, "fail": 0, "rtt": collections.deque(maxlen=150)}
wifi = {"rssi": None, "bssid": None, "channel": None, "name": None, "seen": None, "at": None}
settings = {"lbPerAugerHour": 4.0, "augerTotal": 0.0, "hopper": None, "cookBoot": None,
            "grill": None, "apNames": {}}
cfg = {"board": None, "demo": False, "wifiScanEvery": 600, "persist": True, "phoneUrl": None}


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            settings.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_settings():
    if not cfg["persist"]:
        return
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=1)
    os.replace(tmp, SETTINGS_PATH)


def http_rpc(grill, method, timeout=4):
    with urllib.request.urlopen(f"http://{grill}/rpc/{method}", timeout=timeout) as r:
        return json.loads(r.read())


rpc = http_rpc


def parts_of(hexstr):
    try:
        return list(bytes.fromhex(hexstr or ""))
    except ValueError:
        return []


def temp(p, i):
    v = p[i] * 100 + p[i + 1] * 10 + p[i + 2]
    return None if v == NO_PROBE else v


def decode(state, board):
    out = {}
    p = parts_of(state.get("sc_12"))
    if len(p) >= board["tlen"] and p[1] == 0x0C:
        for key, i in board["temps"].items():
            v = temp(p, i)
            out[key] = (v or None) if key.endswith("Target") else v
        out["fahrenheit"] = p[board["fahrenheit"]] == 1
    s = parts_of(state.get("sc_11"))
    if len(s) >= board["slen"] and s[1] == 0x0B:
        for key, i in board["status"].items():
            out[key] = s[i] == 1
        out["errors"] = [name for i, name in board["errors"].items() if s[i] == 1]
    return out


def encode(values, board):
    """Inverse of decode(), used by the demo simulator and the tests."""
    def frame(kind, length):
        b = [0] * length
        b[0], b[1] = 0xFE, kind
        return b
    t = frame(0x0C, board["tlen"])
    for key, i in board["temps"].items():
        v = values.get(key)
        v = NO_PROBE if v is None and not key.endswith("Target") else (v or 0)
        t[i:i + 3] = [v // 100, v // 10 % 10, v % 10]
    t[board["fahrenheit"]] = 1
    s = frame(0x0B, board["slen"])
    for key, i in board["status"].items():
        s[i] = int(bool(values.get(key)))
    for i, name in board["errors"].items():
        s[i] = int(name in values.get("errors", []))
    return {"sc_12": bytes(t + [0xFF]).hex().upper(), "sc_11": bytes(s + [0xFF]).hex().upper()}


def log_path(ts):
    return os.path.join(LOG_DIR, datetime.fromtimestamp(ts).strftime("%Y-%m-%d") + ".csv")


def append_csv(row):
    if not cfg["persist"]:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    path = log_path(row["t"])
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        w.writerow([("|".join(row[k]) if k == "errors" else
                     (int(row[k]) if isinstance(row[k], bool) else ("" if row[k] is None else row[k])))
                    for k in FIELDS])


def load_today():
    path = log_path(time.time())
    if not os.path.exists(path):
        return
    cutoff = time.time() - 12 * 3600
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            t = float(r["t"])
            if t < cutoff:
                continue
            row = {"t": t}
            for k in FIELDS[1:]:
                v = r.get(k, "")
                if k in ("on", "fan", "auger", "igniter", "light", "prime"):
                    row[k] = v == "1"
                elif k == "errors":
                    row[k] = [e for e in v.split("|") if e]
                else:
                    row[k] = int(v) if v else None
            samples.append(row)


def is_grill(info):
    app = str(info.get("app", ""))
    return app[:2] in ("PB", "LB", "LF")


def lan_ip():
    """This computer's address on the home network (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def discover():
    """Find the grill on this machine's /24: one HTTP probe per address, nothing heavier
    (the ESP32 in the grill does not cope with aggressive scanning)."""
    me = lan_ip()
    if not me:
        return None
    base = me.rsplit(".", 1)[0]
    print(f"Looking for a Pit Boss grill on {base}.0/24 ...", flush=True)

    def probe(ip):
        c = socket.socket()
        c.settimeout(0.5)
        try:
            if c.connect_ex((ip, 80)) != 0:
                return None
        finally:
            c.close()
        try:
            info = http_rpc(ip, "Sys.GetInfo", timeout=3)
            return (ip, info) if is_grill(info) else None
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(32) as ex:
        for hit in ex.map(probe, [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != me]):
            if hit:
                return hit
    return None


def ingest(d, now, last, prev):
    last.update(d)
    row = {k: last.get(k) for k in FIELDS[1:]}
    row["t"] = round(now, 1)
    row["errors"] = last.get("errors", [])
    with lock:
        if row.get("on") is False:
            settings["cookBoot"] = None
        if prev and prev.get("auger") and row["t"] - prev["t"] < 15:
            settings["augerTotal"] += row["t"] - prev["t"]
        samples.append(row)
        if len(samples) > 30000:
            del samples[:5000]
        latest.update(sample=row, error=None, lastOk=now, fahrenheit=last.get("fahrenheit", True))
    append_csv(row)
    return row


def read_info(grill, now):
    info = rpc(grill, "Sys.GetInfo")
    board_id = str(info.get("app", "?"))
    with lock:
        latest["info"] = {k: info.get(k) for k in ("fw_id", "mg_version", "uptime", "ram_free",
                                                   "ram_min_free", "ram_size", "arch")}
        latest["info"].update(id=info.get("id"), ssid=(info.get("wifi") or {}).get("ssid"), board=board_id,
                              tested=board_id in TESTED_BOARDS, known=board_id in BOARDS)
        try:
            latest["info"]["pbFirmware"] = rpc(grill, "PB.GetFirmwareVersion").get("firmwareVersion")
        except Exception:
            latest["info"]["pbFirmware"] = None
        boot = now - (info.get("uptime") or 0)
        if (latest.get("sample") or {}).get("on"):
            settings["cookBoot"] = min(settings.get("cookBoot") or boot, boot)
    if not cfg["board"]:
        cfg["board"] = BOARDS.get(board_id, _A)


def wifi_step(grill, state):
    """Non-blocking scan: start one every wifiScanEvery seconds, collect it on a later poll."""
    now = time.time()
    if not state["pending"]:
        if cfg["wifiScanEvery"] and now - state["last"] >= cfg["wifiScanEvery"]:
            rpc(grill, "PBL.StartWifiScan")
            state.update(pending=True, last=now)
        return
    res = rpc(grill, "PBL.GetWifiScanStatus")
    if res.get("results") is None:
        if now - state["last"] > 30:
            state["pending"] = False
        return
    state["pending"] = False
    ssid = (latest.get("info") or {}).get("ssid")
    mine = [r for r in res["results"] if r.get("ssid") == ssid]
    best = max(mine, key=lambda r: r.get("rssi", -999)) if mine else None
    with lock:
        wifi.update(seen=len(res["results"]), at=now)
        if best:
            bssid = best.get("bssid", "")
            wifi.update(rssi=best.get("rssi"), bssid=bssid, channel=best.get("channel"),
                        name=settings.get("apNames", {}).get(bssid.lower()))


def poll(grill, interval):
    last_info = last_save = 0
    last, prev = {}, None
    scan = {"pending": False, "last": 0}
    while True:
        now = time.time()
        # the ESP32 stalls for a few seconds while it scans; don't poll (or time) it meanwhile
        if scan["pending"] and now - scan["last"] < 6:
            time.sleep(1)
            continue
        try:
            if now - last_info > 30 or not cfg["board"]:
                read_info(grill, now)
                last_info = now
            t0 = time.perf_counter()
            state = rpc(grill, "PB.GetState")
            rtt = (time.perf_counter() - t0) * 1000
            with lock:
                link["ok"] += 1
                link["rtt"].append(rtt)
                latest["raw"] = state
            d = decode(state, cfg["board"])
            if d:
                prev = ingest(d, now, last, prev)
            wifi_step(grill, scan)
            if now - last_save > 60:
                with lock:
                    save_settings()
                last_save = now
        except Exception as e:
            with lock:
                link["fail"] += 1
                latest["error"] = f"{type(e).__name__}: {e}"
        time.sleep(max(0.2, interval - (time.time() - now)))


class SimGrill:
    """A pretend PBA grill for --demo: a long smoke with a probe, a set-point bump and a lid opening."""

    def __init__(self, start):
        self.start = self.t = start
        self.g, self.fire, self.p1 = 72.0, 0.0, 41.0
        self.auger = self.igniter = False
        self.events = {"bump": start + 5400, "lid": start + 7000}

    def setpoint(self, t):
        return 275 if t >= self.events["bump"] else 225

    def step_to(self, t):
        while self.t < t:
            dt = min(2.0, t - self.t)
            self.t += dt
            set_ = self.setpoint(self.t)
            age = self.t - self.start
            duty = max(0.08, min(1.0, 0.17 + set_ / 1000 + 0.03 * (set_ - self.g)))
            self.auger = (self.t % 20) < duty * 20 if age > 60 else False
            self.igniter = 30 < age < 300
            self.fire += ((1.0 if self.auger else 0.0) + (0.6 if self.igniter else 0) - self.fire) * dt / 90
            self.g += (0.62 * self.fire - (self.g - 72) / 600) * dt
            if abs(self.t - self.events["lid"]) < 25:
                self.g -= 0.8 * dt
            k = 1.3e-4 * (0.3 if 150 < self.p1 < 168 else 1.0)
            if age > 1200:
                self.p1 += (self.g - self.p1) * k * dt
        return self

    def rpc(self, _grill, method):
        now = time.time()
        if method == "PB.GetState":
            time.sleep(random.uniform(0.3, 0.6))  # the real module takes about this long to answer
            self.step_to(now)
            g = int(round(self.g + random.uniform(-0.6, 0.6)))
            vals = {"on": True, "fan": True, "auger": self.auger, "igniter": self.igniter,
                    "grill": max(0, g), "set": self.setpoint(self.t), "p1": int(self.p1) if self.t - self.start > 1200 else None,
                    "p1Target": 203}
            return encode(vals, _A)
        if method == "Sys.GetInfo":
            return {"id": "PBA-DEMO", "app": "PBA", "fw_id": "20221117-180604", "mg_version": "2.17.0",
                    "uptime": int(now - self.start), "ram_free": 57000 + random.randint(-900, 900),
                    "ram_min_free": 31800, "ram_size": 232360, "arch": "esp32", "wifi": {"ssid": "Backyard"}}
        if method == "PB.GetFirmwareVersion":
            return {"firmwareVersion": "0.5.7"}
        if method == "PBL.StartWifiScan":
            return {"scanning": True, "results": None}
        if method == "PBL.GetWifiScanStatus":
            return {"scanning": False, "results": [
                {"ssid": "Backyard", "bssid": "02:00:00:00:00:01", "rssi": -54 + random.randint(-2, 2), "channel": 6},
                {"ssid": "Neighbor", "bssid": "02:00:00:00:00:02", "rssi": -81, "channel": 11}]}
        raise ValueError(method)


def start_demo():
    global rpc
    now = time.time()
    sim = SimGrill(now - 9000)
    cfg.update(demo=True, persist=False, board=_A)
    settings.update(cookBoot=sim.start, hopper=None, apNames={"02:00:00:00:00:01": "patio access point"})
    last, prev, t = {}, None, sim.start
    while t < now - 2:
        sim.step_to(t)
        vals = {"on": True, "fan": True, "auger": sim.auger, "igniter": sim.igniter, "grill": int(round(sim.g)),
                "set": sim.setpoint(t), "p1": int(sim.p1) if t - sim.start > 1200 else None, "p1Target": 203}
        prev = ingest(decode(encode(vals, _A), _A), t, last, prev)
        t += 2
    settings["hopper"] = {"lbs": 20.0, "at": sim.start, "augerAtFill": 0.0}
    rpc = sim.rpc


def slope_per_min(rows, key):
    pts = [(r["t"], r[key]) for r in rows if r.get(key) is not None]
    if len(pts) < 5 or pts[-1][0] - pts[0][0] < 30:
        return None
    t0 = pts[0][0]
    xs = [(t - t0) / 60 for t, _ in pts]
    ys = [v for _, v in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return None if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def auger_rhythm(rows):
    ons, offs = [], []
    for a, b in zip(rows, rows[1:]):
        if b["t"] - a["t"] > 15:
            continue
        if b.get("auger") and not a.get("auger"):
            ons.append(b["t"])
        elif a.get("auger") and not b.get("auger"):
            offs.append(b["t"])
    feeds = []
    for t_on in ons:
        t_off = next((t for t in offs if t > t_on), None)
        if t_off:
            feeds.append(t_off - t_on)
    periods = [y - x for x, y in zip(ons, ons[1:])]
    if len(feeds) < 2 or not periods:
        return None
    span = rows[-1]["t"] - rows[0]["t"]
    return {"on": round(statistics.fmean(feeds), 1), "every": round(statistics.fmean(periods), 1),
            "perMin": round(len(ons) / span * 60, 1) if span > 0 else None, "count": len(ons)}


def lid_events(cook):
    """A fall from holding (>= set-5) to set-15 within 2 minutes at an unchanged set temp."""
    events, ev = [], None
    hold_t = hold_set = None
    for r in cook:
        g, s = r.get("grill"), r.get("set")
        if g is None or not s:
            continue
        if ev is None:
            if g >= s - 5:
                hold_t, hold_set = r["t"], s
            elif hold_t and s == hold_set and g <= s - 15 and r["t"] - hold_t <= 120:
                ev = {"start": hold_t, "set": s, "low": g}
            elif s != hold_set:
                hold_t = None
        else:
            ev["low"] = min(ev["low"], g)
            if s != ev["set"]:
                ev = None
            elif g >= s - 5:
                events.append({"at": ev["start"], "recovery": round(r["t"] - ev["start"]),
                               "drop": ev["set"] - ev["low"]})
                ev, hold_t, hold_set = None, r["t"], s
    return events, ev


def derived():
    now = time.time()
    rows = samples
    if not rows:
        return {}
    cur = rows[-1]
    start = None
    for r in reversed(rows):
        if not r.get("on"):
            break
        start = r["t"]
    cook = [r for r in rows if start and r["t"] >= start]
    # the Wi-Fi module powers up with the grill, so its boot time is the real start
    # when our history begins mid-cook
    if start and start == rows[0]["t"] and settings.get("cookBoot"):
        start = min(start, settings["cookBoot"])
    win = lambda sec: [r for r in rows if r["t"] >= now - sec]
    duty = lambda rs: round(100 * sum(1 for r in rs if r.get("auger")) / len(rs)) if rs else None

    out = {"cookStart": start, "cookSeconds": (now - start) if start else None,
           "augerDuty5": duty(win(300)), "augerDuty15": duty(win(900))}
    if cook:
        dt = [b["t"] - a["t"] for a, b in zip(cook, cook[1:])]
        out["augerOnSeconds"] = round(sum(d for d, a in zip(dt, cook) if a.get("auger") and d < 15))
        grills = [r["grill"] for r in cook if r.get("grill") is not None]
        out["peak"] = max(grills) if grills else None
        out["readings"] = len(cook)

    rate = slope_per_min(win(150), "grill")
    out["heatRate"] = None if rate is None else round(rate, 1)
    g, s = cur.get("grill"), cur.get("set")
    if g is not None and s and rate and abs(rate) >= 0.5 and abs(s - g) > 3 and (s - g) * rate > 0:
        out["etaSet"] = round((s - g) / rate * 60)

    reached = None
    for r in cook:
        if r.get("grill") is not None and r.get("set") and r["grill"] >= r["set"] - 5:
            reached = r["t"]
            break
    out["reachedAt"] = reached
    first = next((r for r in cook if r.get("grill") is not None and r.get("set")), None)
    if reached and start and first and first["grill"] < first["set"] - 5:
        out["preheatSeconds"] = round(reached - start)
    if reached and start:
        hold = [r["grill"] - r["set"] for r in cook if r["t"] >= max(reached, now - 900)
                and r.get("grill") is not None and r.get("set")]
        if len(hold) > 10:
            out["holdAvgDev"] = round(statistics.fmean(abs(x) for x in hold), 1)
            out["holdSwing"] = [min(hold), max(hold)]
        since = [r for r in cook if r["t"] >= reached and r.get("grill") is not None and r.get("set")]
        if len(since) > 10:
            out["inZone"] = round(100 * sum(1 for r in since if abs(r["grill"] - r["set"]) <= 10) / len(since))
        events, open_now = lid_events(cook)
        out["lid"] = {"count": len(events), "last": events[-1] if events else None,
                      "avgRecovery": round(statistics.fmean(e["recovery"] for e in events)) if events else None,
                      "openNow": open_now is not None}

    out["rhythm"] = auger_rhythm(win(900))
    lph = settings["lbPerAugerHour"]
    if out.get("augerOnSeconds") is not None:
        out["pelletsCook"] = round(out["augerOnSeconds"] / 3600 * lph, 2)
    if out["augerDuty15"] is not None and cur.get("on"):
        out["burnRate"] = round(out["augerDuty15"] / 100 * lph, 2)
    hop = settings.get("hopper")
    if hop:
        left = hop["lbs"] - (settings["augerTotal"] - hop["augerAtFill"]) / 3600 * lph
        out["hopper"] = {"left": round(max(0.0, left), 1), "filled": hop["lbs"], "at": hop["at"]}
        if out.get("burnRate"):
            out["hopper"]["emptyIn"] = round(max(0.0, left) / out["burnRate"] * 3600)
    out["lbPerAugerHour"] = lph

    probes = {}
    for key, target in (("p1", "p1Target"), ("p2", "p2Target"), ("p3", None), ("p4", None)):
        if cur.get(key) is None:
            continue
        pr = slope_per_min(win(600), key)
        info = {"rate": None if pr is None else round(pr, 2)}
        tgt = cur.get(target) if target else None
        if tgt and pr and pr > 0.05 and cur[key] < tgt:
            info["eta"] = round((tgt - cur[key]) / pr * 60)
        probes[key] = info
    out["probes"] = probes

    rtts = sorted(link["rtt"])
    total = link["ok"] + link["fail"]
    out["link"] = {"rttAvg": round(statistics.fmean(rtts)) if rtts else None,
                   "rttP95": round(rtts[math.ceil(len(rtts) * 0.95) - 1]) if len(rtts) >= 20 else None,
                   "reliability": round(100 * link["ok"] / total, 1) if total else None,
                   "polls": total}
    out["wifi"] = dict(wifi)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            with lock:
                body = {k: v for k, v in latest.items() if k != "raw"}
                body.update(now=time.time(), demo=cfg["demo"], phoneUrl=cfg["phoneUrl"], derived=derived())
            return self.send(200, json.dumps(body), "application/json")
        if path == "/phone.svg" and cfg["phoneUrl"]:
            return self.send(200, qr.to_svg(cfg["phoneUrl"]), "image/svg+xml")
        if path == "/api/history":
            since = 0.0
            for kv in query.split("&"):
                if kv.startswith("since="):
                    since = float(kv[6:] or 0)
            with lock:
                rows = [r for r in samples if r["t"] > since]
            return self.send(200, json.dumps(rows), "application/json")
        if path == "/api/raw":
            with lock:
                info = latest.get("info") or {}
                body = {"board": info.get("board"), "firmware": info.get("pbFirmware"), "frames": latest.get("raw"),
                        "decoded": latest.get("sample")}
            return self.send(200, json.dumps(body, indent=1), "application/json")
        self.send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/settings":
            return self.send(404, "not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            with lock:
                if "hopperLbs" in body:
                    lbs = float(body["hopperLbs"])
                    settings["hopper"] = None if lbs <= 0 else {
                        "lbs": lbs, "at": time.time(), "augerAtFill": settings["augerTotal"]}
                if "lbPerAugerHour" in body and 0.5 <= float(body["lbPerAugerHour"]) <= 40:
                    settings["lbPerAugerHour"] = float(body["lbPerAugerHour"])
                save_settings()
            self.send(200, json.dumps({"ok": True}), "application/json")
        except (ValueError, TypeError) as e:
            self.send(400, json.dumps({"error": str(e)}), "application/json")


def main():
    ap = argparse.ArgumentParser(description="Live local dashboard for Pit Boss Wi-Fi grills (read-only).")
    ap.add_argument("--grill", help="grill IP or hostname (default: last one found, else search the network)")
    ap.add_argument("--port", type=int, default=8880)
    ap.add_argument("--local-only", action="store_true", help="only this computer can open the dashboard")
    ap.add_argument("--host", help=argparse.SUPPRESS)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between polls (default 2)")
    ap.add_argument("--board", choices=sorted(BOARDS), help="override the control board the grill reports")
    ap.add_argument("--no-wifi-scan", action="store_true", help="skip the 10-minute Wi-Fi signal scans")
    ap.add_argument("--demo", action="store_true", help="simulated cook, no grill needed")
    a = ap.parse_args()
    if a.no_wifi_scan:
        cfg["wifiScanEvery"] = 0
    if a.board:
        cfg["board"] = BOARDS[a.board]

    grill = None
    if a.demo:
        start_demo()
        grill = "demo"
    else:
        load_settings()
        grill = a.grill or settings.get("grill")
        if grill and not a.grill:
            try:
                if not is_grill(http_rpc(grill, "Sys.GetInfo", timeout=3)):
                    grill = None
            except Exception:
                grill = None
        if not grill:
            hit = discover()
            if not hit:
                raise SystemExit("No grill found. Make sure it's switched on and on the same network, "
                                 "or pass --grill <ip>.")
            grill = hit[0]
            print(f"Found {hit[1].get('id')} at {grill}", flush=True)
        settings["grill"] = grill
        save_settings()
        load_today()
    host = a.host or ("127.0.0.1" if a.local_only else "0.0.0.0")
    if host not in ("127.0.0.1", "localhost"):
        ip = lan_ip() if host == "0.0.0.0" else host
        cfg["phoneUrl"] = f"http://{ip}:{a.port}" if ip else None
    server = ThreadingHTTPServer((host, a.port), Handler)
    threading.Thread(target=poll, args=(grill, a.interval), daemon=True).start()
    print(f"\nGrill Live is running (grill: {grill})\n", flush=True)
    print(f"  On this computer:  http://localhost:{a.port}", flush=True)
    if cfg["phoneUrl"]:
        print(f"  On your phone:     {cfg['phoneUrl']}   (same Wi-Fi)\n", flush=True)
        if sys.stdout.isatty():
            if os.name == "nt":
                os.system("")  # turns on colour codes in the Windows console
            try:
                print("  Scan this with your phone's camera:\n", flush=True)
                print(qr.to_terminal(cfg["phoneUrl"]) + "\n", flush=True)
            except UnicodeEncodeError:
                print("  (Open the dashboard on this computer and tap Open on phone for a QR code.)\n", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
