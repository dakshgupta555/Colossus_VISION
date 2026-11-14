"""Serial bridge: reads the ESP32's INA219 printouts over USB and feeds the backend.

Read-only: it never writes to the ESP32 and never uploads anything. DTR/RTS are held low
so opening the port does not reset the board.

Matches the teammate's print format:
    Bus Voltage: 0.836 V
    Shunt Voltage: 0.000 mV
    Current: -0.100 mA
    Power: 0.000 mW
A reading is made when the "Current:" line arrives, using the latest "Bus Voltage:"
(voltage is null if none was seen).

Bench wiring (one panel): a jumper from INA219 VIN- to panel minus = short circuit (V ~ 0, I = Isc);
jumper removed = open circuit (I ~ 0, V = Voc). Panel minus is tied to ESP32 GND.

If the node also prints LDR values ("LDR1: 2345" ...) or a temperature ("Temperature: 28.5 C") they are parsed and
sent along (fields ldr, temp_c). Nothing is invented: a sensor that does not print stays null.

Every reading goes to POST /telemetry (raw value + the state of the 3 s rolling average).
Once the state has been stable for 2 s, the 3 s average also goes to POST /bench/point.
States:  SHORT  V < 0.15 V and I >= 0.15 mA   OPEN  I < 0.15 mA and V > 0.3 V
         DARK   V < 0.15 V and I < 0.15 mA    LOAD  anything else (R = V / I)

Run:  python serial_bridge.py                     (auto-finds /dev/cu.usb*)
      python serial_bridge.py --port /dev/cu.usbserial-0001 --baud 115200
Close the Arduino IDE Serial Monitor first: only one program can hold the port.
"""
import argparse
import collections
import glob
import re
import statistics
import time

import requests
import serial

NUM = re.compile(r"-?\d+(?:\.\d+)?")
LDR_LABEL = re.compile(r"^\s*ldr[\s_\-\[]*(\d+)?\]?\s*$", re.I)   # "LDR", "LDR1", "LDR 2", "LDR_3", "LDR[0]"
V_RE = re.compile(r"Bus Voltage:\s*(-?\d+(?:\.\d+)?)\s*V", re.I)
MA_RE = re.compile(r"^\s*Current:\s*(-?\d+(?:\.\d+)?)\s*mA", re.I)

WINDOW_S = 3.0          # rolling average length
SETTLE_S = 2.0          # state must be unchanged this long before it is posted as a bench point
JUMP_V, JUMP_MA = 0.20, 0.40   # a step bigger than this restarts the window (jumper moved)
V_LOW, V_OPEN = 0.15, 0.30     # volts
I_NOISE = 0.15                 # mA: below this the current counts as zero. The INA219 steps in 0.1 mA, so 0.05 mA flickers


def classify(v, ma):
    """short / open / dark / load from the window averages (v in V, ma in mA)."""
    if v is None:
        return None
    if v < V_LOW:
        return "short" if ma >= I_NOISE else "dark"
    if ma < I_NOISE and v > V_OPEN:
        return "open"
    return "load"


class Window:
    """Rolling 3 s window of readings with state tracking."""

    def __init__(self):
        self.buf = collections.deque()           # (t, v, ma)
        self.state, self.since = None, 0.0

    def add(self, t, v, ma):
        prev_v = [x[1] for x in self.buf if x[1] is not None]         # a reading can arrive without a voltage (started mid-block)
        if v is not None and prev_v:
            mv = statistics.fmean(prev_v)
            mi = statistics.fmean(x[2] for x in self.buf)
            if abs(v - mv) > JUMP_V or abs(ma - mi) > JUMP_MA:
                self.buf.clear()                 # the jumper moved: forget the old values
                self.state = None
        self.buf.append((t, v, ma))
        while self.buf and t - self.buf[0][0] > WINDOW_S:
            self.buf.popleft()
        vs = [x[1] for x in self.buf if x[1] is not None]
        mas = [x[2] for x in self.buf]
        v_avg = statistics.fmean(vs) if vs else None
        ma_avg = statistics.fmean(mas)
        spread = statistics.pstdev(mas) if len(mas) > 1 else 0.0
        st = classify(v_avg, ma_avg)
        if st != self.state:
            self.state, self.since = st, t
        settled = st is not None and len(self.buf) >= 2 and t - self.since >= SETTLE_S
        return dict(state=st, settled=settled, v_avg=v_avg, ma_avg=ma_avg, spread=spread, n=len(self.buf))


def parse_extra(line, ldr_seq):
    """LDR / temperature lines, if the node prints them. Returns (kind, value) or None.
    LDR: 'LDR1: 2345', 'LDR 2 = 1500', 'LDR: 1200, 1800, 900'   -> ("ldr", {index: raw})
    Temperature: 'Temperature: 28.5 C', 'Temp = 31.2'             -> ("temp", celsius)"""
    low = line.lower()
    if ":" not in line and "=" not in line:
        return None
    label, _, rest = re.split(r"([:=])", line, maxsplit=1)[0], None, re.split(r"[:=]", line, maxsplit=1)[1]
    vals = [float(x) for x in NUM.findall(rest)]
    if not vals:
        return None
    if "ldr" in label.lower():
        if len(vals) > 1:
            return "ldr", {i: v for i, v in enumerate(vals)}
        m = LDR_LABEL.match(label)
        idx = int(m.group(1)) - 1 if m and m.group(1) is not None else ldr_seq
        return "ldr", {max(0, idx): vals[0]}
    if "temp" in label.lower():
        c = vals[0]
        if re.search(r"\bf\b|°f", rest.lower()):
            c = (c - 32) * 5 / 9
        return "temp", c
    return None


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbserial*") + glob.glob("/dev/cu.SLAB_USBtoUART*")
                   + glob.glob("/dev/cu.wchusbserial*") + glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


def open_port(port, baud):
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, baud, 2
    s.dtr = False          # set before open() so the ESP32 is not reset
    s.rts = False
    s.open()
    return s


def post(url, body):
    try:
        requests.post(url, json=body, timeout=3).raise_for_status()
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--node", default="SN-001")
    a = ap.parse_args()

    n = 0
    win = Window()
    while True:                                   # outer loop: reconnect after unplug
        port = a.port or find_port()
        if not port:
            print("no ESP32 serial port found (looking for /dev/cu.usb*). Plug it in; retrying in 2 s", flush=True)
            time.sleep(2)
            continue
        try:
            ser = open_port(port, a.baud)
        except serial.SerialException as exc:
            print(f"cannot open {port}: {exc}. Close the Arduino IDE Serial Monitor if it is open. Retrying in 2 s",
                  flush=True)
            time.sleep(2)
            continue
        print(f"connected to {port} at {a.baud} baud, posting to {a.url} as {a.node}", flush=True)
        v, win = None, Window()
        ldr, ldr_t, temp, temp_t, ldr_seq = {}, 0.0, None, 0.0, 0
        try:
            while True:
                line = ser.readline().decode("utf-8", "replace")
                if not line:
                    continue
                m = V_RE.search(line)
                if m:
                    v = float(m.group(1))
                    ldr_seq = 0
                    continue
                ex = parse_extra(line, ldr_seq)
                if ex:
                    if ex[0] == "ldr":
                        ldr.update(ex[1]); ldr_t = time.time(); ldr_seq = max(ex[1]) + 1
                    else:
                        temp, temp_t = ex[1], time.time()
                    continue
                m = MA_RE.match(line)
                if not m:
                    continue
                ma, t = float(m.group(1)), time.time()
                try:
                    w = win.add(t, v, ma)
                except Exception as exc:                  # never let one odd reading kill the bridge
                    print(f"bad reading skipped ({exc.__class__.__name__}: {exc}); window reset", flush=True)
                    win = Window()
                    v = None
                    continue
                n += 1
                fresh = lambda t0: t - t0 < 5.0          # an old LDR/temperature value is dropped, never reused
                ldr_list = [ldr.get(i) for i in range(max(ldr) + 1)] if ldr and fresh(ldr_t) else None
                ok = post(a.url + "/telemetry", dict(
                    node_id=a.node, source="real", v=v, ma=ma, raw=line.strip(), state=w["state"],
                    settled=w["settled"], v_avg=w["v_avg"], ma_avg=w["ma_avg"], spread=w["spread"],
                    ldr=ldr_list, temp_c=temp if (temp is not None and fresh(temp_t)) else None))
                sent = ""
                if w["settled"] and w["state"] and w["v_avg"] is not None:
                    ok = post(a.url + "/bench/point", dict(
                        node_id=a.node, state=w["state"], v=w["v_avg"], ma=w["ma_avg"], spread=w["spread"],
                        n=w["n"])) and ok
                    sent = " +bench point"
                vtxt = "null" if v is None else f"{v:.3f} V"
                st = (w["state"] or "?").upper() + ("" if w["settled"] else " (settling)")
                print(f"#{n} {time.strftime('%H:%M:%S')} v={vtxt} i={ma:.1f} mA | 3s avg i={w['ma_avg']:.2f} mA "
                      f"spread={w['spread']:.2f} | {st}{sent}"
                      + (f" | ldr={ldr_list}" if ldr_list else "") + (f" | T={temp:.1f}C" if temp is not None and fresh(temp_t) else "")
                      + f" -> {'sent' if ok else 'POST failed'}", flush=True)
                v = None
        except (serial.SerialException, OSError) as exc:
            print(f"serial lost ({exc.__class__.__name__}); reconnecting in 2 s", flush=True)
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbridge stopped")
