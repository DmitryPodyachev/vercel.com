#!/bin/sh
# Rebuild the template firmwares (only needed when templates/src changes). Needs arduino-cli with the cores:
#   arduino-cli core install esp8266:esp8266 rp2040:rp2040 esp32:esp32   (board manager URLs below)
#   https://arduino.esp8266.com/stable/package_esp8266com_index.json
#   https://espressif.github.io/arduino-esp32/package_esp32_index.json
#   https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json
# Built with: esp32 4.0.0, esp8266 3.1.2, rp2040 6.4.0.
set -e
cd "$(dirname "$0")"
c() { arduino-cli compile -b "$1" --build-property "compiler.cpp.extra_flags=-DBHF_DEFAULT_PIN=$2 -DBHF_ACTIVE_LOW=$3" --output-dir "out/$4" src/bhf_template; }
c rp2040:rp2040:rpipico 25 0 pico && cp out/pico/bhf_template.ino.uf2 rpi_pico.uf2
c esp8266:esp8266:nodemcuv2 2 1 esp8266 && cp out/esp8266/bhf_template.ino.bin esp8266.bin
c esp32:esp32:esp32cam 33 1 esp32cam
cp out/esp32cam/bhf_template.ino.bin esp32_cam/app.bin
cp out/esp32cam/bhf_template.ino.bootloader.bin esp32_cam/bootloader.bin
cp out/esp32cam/bhf_template.ino.partitions.bin esp32_cam/partitions.bin
cp "$(dirname "$(arduino-cli config get directories.data)")"/*/packages/esp32/hardware/esp32/*/tools/partitions/boot_app0.bin esp32_cam/ 2>/dev/null || true
rm -rf out
