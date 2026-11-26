// bhf.pp.ua template firmware (ESP32, ESP8266, RP2040).
// Built once with arduino-cli; the builder on Vercel patches the BhfCfg block in the binary
// (pin, interval, signature, job id, message) and recomputes the image checksums.
// So the same compiled code runs for every job – only the data block differs.
#include <Arduino.h>

struct BhfCfg {
  char magic[8];          // "BHFCFG01"
  uint32_t size;          // sizeof(BhfCfg) – checked by the patcher
  int32_t pin;            // LED pin (GPIO number)
  uint32_t ms;            // half period
  uint8_t signature;      // 5 quick blinks at start
  uint8_t active_low;     // LED lights when the pin is LOW (ESP-12 blue LED, ESP32-CAM red LED)
  uint8_t r1, r2;
  char fw[16];            // "blink" / "blink_fast"
  char job[44];           // job id
  char msg[16384];        // UTF-8, 0-terminated
};

#if defined(ESP8266)
#define BHF_FLASH PROGMEM
#else
#define BHF_FLASH
#endif

extern const BhfCfg bhf_cfg;
__attribute__((used, aligned(4))) const BhfCfg bhf_cfg BHF_FLASH = {
  {'B', 'H', 'F', 'C', 'F', 'G', '0', '1'}, sizeof(BhfCfg), BHF_DEFAULT_PIN, 2000, 0, BHF_ACTIVE_LOW, 0, 0,
  "blink", "-", "template",
};

// read from the block without letting the compiler fold the template values into the code
static void cfgRead(void *dst, size_t off, size_t n) {
  const uint8_t *p = (const uint8_t *)&bhf_cfg;
  asm volatile("" : "+r"(p));
#if defined(ESP8266)
  memcpy_P(dst, p + off, n);
#else
  memcpy(dst, p + off, n);
#endif
}

static int32_t ledPin;
static uint32_t halfMs;
static uint8_t activeLow;
static char fwName[17], jobId[45];

static void led(bool on) { digitalWrite(ledPin, (on ^ (activeLow != 0)) ? HIGH : LOW); }

static void printMessage() {
  char buf[65];
  for (size_t off = 0; off < sizeof(bhf_cfg.msg); off += 64) {
    cfgRead(buf, offsetof(BhfCfg, msg) + off, 64);
    buf[64] = 0;
    size_t n = strlen(buf);
    Serial.write((const uint8_t *)buf, n);
    if (n < 64) break;
  }
  Serial.println();
}

void setup() {
  BhfCfg head;
  cfgRead(&head, 0, offsetof(BhfCfg, msg));
  ledPin = head.pin; halfMs = head.ms; activeLow = head.active_low;
  memcpy(fwName, head.fw, 16); fwName[16] = 0;
  memcpy(jobId, head.job, 44); jobId[44] = 0;
  pinMode(ledPin, OUTPUT);
  led(false);
  Serial.begin(115200);
#if defined(ARDUINO_ARCH_RP2040)
  for (uint32_t t = millis(); !Serial && millis() - t < 3000;) delay(10);   // USB serial: wait for the host
#else
  delay(200);
  Serial.println();
#endif
  Serial.printf("bhf %s job %s: LED pin %ld toggles every %lu ms\r\n", fwName, jobId, (long)ledPin, (unsigned long)halfMs);
  printMessage();
  if (head.signature) {
    for (int i = 0; i < 5; i++) { led(true); delay(100); led(false); delay(100); }
    delay(500);
  }
}

static unsigned long n = 0;

void loop() {
  led(true);
  delay(halfMs);
  led(false);
  delay(halfMs);
  if (++n % 10 == 0) {
    Serial.printf("blink %lu\r\n", n);
    printMessage();
  }
}
