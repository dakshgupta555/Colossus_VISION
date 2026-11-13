# COLOSSUS VISION

ESP32-based solar/power monitoring prototype using an INA219 current/voltage sensor and an I2C OLED display.

## Repository status

This repository currently contains the firmware baseline for the hardware prototype. The firmware is a **prototype release candidate** and must be compiled and verified against the exact ESP32, INA219 breakout, OLED controller/address, and power source before being described as field-tested or deployment-ready.

## Hardware baseline

- ESP32 DevKit (classic ESP32)
- INA219 current/voltage sensor breakout
- 128×64 SSD1306 I2C OLED
- Solar panel/source and a DC load connected through the INA219 high-side measurement path

## Wiring

Both I2C devices share the ESP32 I2C bus:

| ESP32 | INA219 | OLED |
|---|---|---|
| 3V3 | VCC | VCC |
| GND | GND | GND |
| GPIO 21 | SDA | SDA |
| GPIO 22 | SCL | SCL |

The I2C address is normally `0x40` for INA219 and `0x3C` for SSD1306 OLED. Some OLED modules use `0x3D`; change `OLED_ADDRESS` in the sketch if required.

### INA219 measurement path

- Source positive → INA219 `VIN+`
- INA219 `VIN−` → load positive
- Source negative and load negative → common ground

`VIN+` and `VIN−` are measurement-path terminals, not ESP32 GPIO pins. Check the breakout's voltage/current ratings and the source/load specifications before connecting. Do not connect a solar panel directly to a battery without a suitable charge controller and protection circuitry.

## Firmware setup

1. Install Arduino IDE and the ESP32 board package.
2. Select **ESP32 Dev Module** and the correct serial port.
3. Install these libraries using Arduino IDE Library Manager:
   - Adafruit INA219
   - Adafruit SSD1306
   - Adafruit GFX Library
4. Open `firmware/colossus_esp32/colossus_esp32.ino`.
5. Verify `OLED_ADDRESS` (`0x3C` or `0x3D`) and the wiring.
6. Upload to the ESP32.
7. Open Serial Monitor at **115200 baud**.

## Telemetry

The firmware samples once per second and reports:

- Bus voltage (V)
- Shunt voltage (mV)
- Current (mA)
- Estimated load voltage (V)
- Power (mW)
- Device status

The OLED shows bus voltage, current, power, and estimated load voltage. If the INA219 is not detected, the firmware reports the fault over Serial and displays a sensor warning when the OLED is available.

## Validation checklist

- [ ] Confirm all grounds are common.
- [ ] Confirm both I2C devices are detected on GPIO 21/22.
- [ ] Confirm the OLED address with an I2C scanner.
- [ ] Confirm INA219 breakout shunt value and current range.
- [ ] Compare voltage readings with a multimeter.
- [ ] Validate current readings with a known safe DC load.
- [ ] Confirm polarity before connecting the solar source and load.
- [ ] Test under actual operating conditions before claiming final validation.

## Known limitations

- No battery charging control, MPPT, over-current protection, or autonomous shutdown is implemented.
- No Wi-Fi/cloud/dashboard publishing is included in this firmware baseline.
- OLED controller and address must match the actual module.
- INA219 calibration must match the breakout and expected current range for accurate readings.
