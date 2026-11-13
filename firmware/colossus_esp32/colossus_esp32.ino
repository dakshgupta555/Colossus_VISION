/*
 * COLOSSUS VISION — ESP32 Solar Monitor Firmware
 * Prototype release candidate v1.0.0
 *
 * Hardware baseline:
 *   - ESP32 DevKit (classic ESP32)
 *   - INA219 current/voltage sensor (I2C address normally 0x40)
 *   - SSD1306 128x64 I2C OLED (address normally 0x3C or 0x3D)
 *
 * I2C wiring (shared bus):
 *   ESP32 GPIO 21 (SDA) -> INA219 SDA and OLED SDA
 *   ESP32 GPIO 22 (SCL) -> INA219 SCL and OLED SCL
 *   ESP32 3V3             -> both module VCC pins (only if modules support 3.3V)
 *   ESP32 GND             -> both module GND pins
 *
 * IMPORTANT POWER PATH:
 *   Source positive -> INA219 VIN+
 *   INA219 VIN-      -> load positive
 *   Source negative and load negative -> common GND
 *   Do NOT connect VIN+/VIN- to ESP32 GPIO pins. Check source/load voltage
 *   and INA219 board limits before connecting. This sketch does not manage
 *   battery charging or power-path protection.
 *
 * Arduino IDE libraries (Library Manager):
 *   - Adafruit INA219
 *   - Adafruit SSD1306
 *   - Adafruit GFX Library (dependency)
 *
 * Serial output: 115200 baud, CSV-like telemetry once per second.
 */

#include <Wire.h>
#include <Adafruit_INA219.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ---------------------------- Configuration -----------------------------
constexpr uint8_t I2C_SDA_PIN = 21;
constexpr uint8_t I2C_SCL_PIN = 22;
constexpr uint8_t OLED_ADDRESS = 0x3C;  // Change to 0x3D if your scanner reports 0x3D.
constexpr int OLED_WIDTH = 128;
constexpr int OLED_HEIGHT = 64;
constexpr int OLED_RESET = -1;
constexpr uint32_t SAMPLE_INTERVAL_MS = 1000;

// INA219 calibration: Adafruit default calibration is suitable for many
// low-current demonstrations. Select a different calibration only after
// confirming your module's shunt resistor and expected current range.

Adafruit_INA219 ina219;
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, OLED_RESET);

bool ina219Ready = false;
bool oledReady = false;
uint32_t lastSampleMs = 0;

struct Measurements {
  float busVoltageV = NAN;
  float shuntVoltageMv = NAN;
  float currentMa = NAN;
  float loadVoltageV = NAN;
  float powerMw = NAN;
};

// ------------------------------- Helpers --------------------------------
void printHeader() {
  Serial.println(F("timestamp_ms,bus_voltage_V,shunt_voltage_mV,current_mA,load_voltage_V,power_mW,status"));
}

Measurements readMeasurements() {
  Measurements m;
  if (!ina219Ready) return m;

  m.busVoltageV = ina219.getBusVoltage_V();
  m.shuntVoltageMv = ina219.getShuntVoltage_mV();
  m.currentMa = ina219.getCurrent_mA();
  m.powerMw = ina219.getPower_mW();
  m.loadVoltageV = m.busVoltageV + (m.shuntVoltageMv / 1000.0f);
  return m;
}

void updateDisplay(const Measurements &m) {
  if (!oledReady) return;

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println(F("COLOSSUS VISION"));
  display.drawLine(0, 10, OLED_WIDTH - 1, 10, SSD1306_WHITE);

  if (!ina219Ready) {
    display.setCursor(0, 22);
    display.println(F("INA219 NOT FOUND"));
    display.println(F("Check I2C wiring"));
  } else {
    display.setCursor(0, 15);
    display.print(F("Bus: "));
    display.print(m.busVoltageV, 2);
    display.println(F(" V"));

    display.setCursor(0, 27);
    display.print(F("Current: "));
    display.print(m.currentMa, 1);
    display.println(F(" mA"));

    display.setCursor(0, 39);
    display.print(F("Power: "));
    display.print(m.powerMw, 1);
    display.println(F(" mW"));

    display.setCursor(0, 52);
    display.print(F("Load: "));
    display.print(m.loadVoltageV, 2);
    display.print(F(" V"));
  }

  display.display();
}

void logMeasurements(const Measurements &m) {
  Serial.print(millis());
  Serial.print(',');

  if (!ina219Ready) {
    Serial.println(F(",,,,,INA219_MISSING"));
    return;
  }

  Serial.print(m.busVoltageV, 3); Serial.print(',');
  Serial.print(m.shuntVoltageMv, 3); Serial.print(',');
  Serial.print(m.currentMa, 3); Serial.print(',');
  Serial.print(m.loadVoltageV, 3); Serial.print(',');
  Serial.print(m.powerMw, 3); Serial.println(F(",OK"));
}

// ------------------------------- Setup -----------------------------------
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println();
  Serial.println(F("COLOSSUS VISION ESP32 Solar Monitor v1.0.0"));
  Serial.println(F("Starting I2C bus..."));

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(100000);  // Conservative standard-mode I2C clock.

  oledReady = display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS);
  if (oledReady) {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.println(F("COLOSSUS VISION"));
    display.println(F("Initializing..."));
    display.display();
    Serial.println(F("OLED: detected"));
  } else {
    Serial.println(F("OLED: not detected; continuing on Serial"));
  }

  ina219Ready = ina219.begin();
  if (ina219Ready) {
    Serial.println(F("INA219: detected"));
  } else {
    Serial.println(F("INA219: not detected; check VCC/GND/SDA/SCL"));
  }

  Serial.print(F("I2C SDA: GPIO ")); Serial.println(I2C_SDA_PIN);
  Serial.print(F("I2C SCL: GPIO ")); Serial.println(I2C_SCL_PIN);
  printHeader();

  if (oledReady) {
    display.clearDisplay();
    display.setCursor(0, 0);
    display.println(F("COLOSSUS VISION"));
    display.println(ina219Ready ? F("Sensor connected") : F("Sensor missing"));
    display.println(F("Telemetry starting"));
    display.display();
  }
}

// -------------------------------- Loop -----------------------------------
void loop() {
  const uint32_t now = millis();
  if (now - lastSampleMs < SAMPLE_INTERVAL_MS) return;
  lastSampleMs = now;

  const Measurements m = readMeasurements();
  logMeasurements(m);
  updateDisplay(m);
}
