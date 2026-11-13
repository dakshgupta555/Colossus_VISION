"""Serial bridge: reads the ESP32's INA219 printouts over USB and POSTs them to /telemetry.

Read-only: it never writes to the ESP32 and never uploads anything. DTR/RTS are held low
so opening the port does not reset the board.

Matches the teammate's print format:
    Bus Voltage: 0.836 V
    Shunt Voltage: 0.000 mV
    Current: -0.100 mA
    Power: 0.000 mW
A reading is sent when the "Current:" line arrives, using the latest "Bus Voltage:"
(voltage is sent as null if none was seen).

Run:  python serial_bridge.py                     (auto-finds /dev/cu.usb*)
      python serial_bridge.py --port /dev/cu.usbserial-0001 --baud 115200
Close the Arduino IDE Serial Monitor first: only one program can hold the port.
"""
import argparse
import glob
import re
import time

import requests
import serial

V_RE = re.compile(r"Bus Voltage:\s*(-?\d+(?:\.\d+)?)\s*V", re.I)
MA_RE = re.compile(r"^\s*Current:\s*(-?\d+(?:\.\d+)?)\s*mA", re.I)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--node", default="SN-001")
    a = ap.parse_args()

    n = 0
    while True:                                   # outer loop: reconnect after unplug
        port = a.port or find_port()
        if not port:
            print("no ESP32 serial port found (looking for /dev/cu.usb*). Plug it in; retrying in 2 s")
            time.sleep(2)
            continue
        try:
            ser = open_port(port, a.baud)
        except serial.SerialException as exc:
            print(f"cannot open {port}: {exc}. Close the Arduino IDE Serial Monitor if it is open. Retrying in 2 s")
            time.sleep(2)
            continue
        print(f"connected to {port} at {a.baud} baud, posting to {a.url}/telemetry as {a.node}")
        v = None
        try:
            while True:
                line = ser.readline().decode("utf-8", "replace")
                if not line:
                    continue
                m = V_RE.search(line)
                if m:
                    v = float(m.group(1))
                    continue
                m = MA_RE.match(line)
                if not m:
                    continue
                ma = float(m.group(1))
                n += 1
                try:
                    requests.post(a.url + "/telemetry", timeout=3,
                                  json=dict(node_id=a.node, source="real", v=v, ma=ma)).raise_for_status()
                    state = "sent"
                except Exception as exc:
                    state = f"POST failed ({exc.__class__.__name__})"
                vtxt = "null" if v is None else f"{v:.3f} V"
                print(f"#{n} {time.strftime('%H:%M:%S')} v={vtxt} i={ma:.3f} mA -> {state}")
                v = None
        except (serial.SerialException, OSError) as exc:
            print(f"serial lost ({exc.__class__.__name__}); reconnecting in 2 s")
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
