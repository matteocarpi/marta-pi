/*
 * Text and image renderer for a Waveshare 7.5" 800x480 B/W e-paper panel on the Waveshare
 * e-Paper ESP32 Driver Board.
 *
 * Reads newline-terminated commands from USB serial and draws them. The panel
 * never decides anything; the Raspberry Pi side (../epaper.py) sends text or bitmaps.
 *
 * Protocol (one command per line, replies are one line each):
 *   PING           -> PONG
 *   CLEAR          -> OK          blank the screen
 *   FONT S|M|L     -> OK          12 / 18 / 24 pt, applies to the next TEXT
 *   TEXT <string>  -> OK          word-wrapped and centred; \n starts a new line
 *   IMAGE <w> <h>  -> SEND        then w*h/8 raw bytes follow, 1 bit per pixel,
 *                  -> OK          rows MSB first, 1 = white; drawn full screen
 * Anything else    -> ERR <why>
 * On boot the sketch emits READY once the panel is initialised.
 *
 * Board in Arduino IDE: "ESP32 Dev Module" (the driver board is a plain
 * ESP32-WROOM-32). Library: GxEPD2 by Jean-Marc Zingg, via Library Manager,
 * which pulls in Adafruit GFX for the fonts.
 */

#include <SPI.h>
#include <GxEPD2_BW.h>
#include <Fonts/FreeSansBold12pt7b.h>
#include <Fonts/FreeSansBold18pt7b.h>
#include <Fonts/FreeSansBold24pt7b.h>

// --- Wiring ----------------------------------------------------------------
// Fixed pin map of the e-Paper ESP32 Driver Board - nothing to wire by hand.
#define EPD_CS 15
#define EPD_DC 27
#define EPD_RST 26
#define EPD_BUSY 25

// The board routes SPI to pins that are not the ESP32 defaults, so the bus has
// to be re-begun after display.init(). MISO is unused by the panel but
// SPI.begin() wants a pin for it.
#define EPD_SCK 13
#define EPD_MISO 12
#define EPD_MOSI 14

// --- Panel -----------------------------------------------------------------
// GDEW075T7 = 7.5" V2, 800x480, black/white. A recently manufactured panel may
// be the GDEY075T7 instead: if the screen stays blank or comes out as noise,
// swap both names below for GxEPD2_750_GDEY075T7.
//
// The second template argument is the page height. 800x480 mono is 48000 bytes,
// which fits ESP32 RAM in one go, so the whole frame is a single page and the
// firstPage()/nextPage() loop below runs exactly once.
GxEPD2_BW<GxEPD2_750_T7, GxEPD2_750_T7::HEIGHT> display(
    GxEPD2_750_T7(EPD_CS, EPD_DC, EPD_RST, EPD_BUSY));

// --- Layout ----------------------------------------------------------------
const int ROTATION = 1;  // 0 = landscape 800x480, 1 = portrait 480x800, 3 = portrait flipped
const int MARGIN = 24;   // px kept clear on the left and right
const int MAX_LINES = 20;
const size_t MAX_COMMAND = 600;  // longer lines are truncated

const size_t IMAGE_BYTES = (size_t)GxEPD2_750_T7::WIDTH * GxEPD2_750_T7::HEIGHT / 8;
const unsigned long IMAGE_TIMEOUT_MS = 15000;  // 48000 bytes at 115200 baud is ~4 s
uint8_t g_image[IMAGE_BYTES];

const GFXfont *g_font = &FreeSansBold24pt7b;

String g_lines[MAX_LINES];
int g_lineCount = 0;

char g_buffer[MAX_COMMAND];
size_t g_length = 0;
bool g_truncated = false;

uint16_t textWidth(const String &text) {
  int16_t x1, y1;
  uint16_t w, h;
  display.getTextBounds(text, 0, 0, &x1, &y1, &w, &h);
  return w;
}

// Break one paragraph into as many g_lines entries as it needs. A single word
// wider than the usable width is left to overflow rather than hyphenated.
void wrapParagraph(const String &paragraph) {
  const int usable = display.width() - 2 * MARGIN;
  const int length = paragraph.length();

  if (length == 0) {
    if (g_lineCount < MAX_LINES) g_lines[g_lineCount++] = "";
    return;
  }

  String current = "";
  int start = 0;
  while (start <= length && g_lineCount < MAX_LINES) {
    int space = paragraph.indexOf(' ', start);
    String word =
        (space < 0) ? paragraph.substring(start) : paragraph.substring(start, space);
    String candidate = current.length() ? current + " " + word : word;

    if (current.length() == 0 || textWidth(candidate) <= usable) {
      current = candidate;
    } else {
      g_lines[g_lineCount++] = current;
      current = word;
    }

    if (space < 0) break;
    start = space + 1;
  }

  if (current.length() && g_lineCount < MAX_LINES) g_lines[g_lineCount++] = current;
}

// Split on the two-character sequence \n, then wrap each piece.
void layout(const String &text) {
  g_lineCount = 0;
  int start = 0;
  while (start <= (int)text.length()) {
    int brk = text.indexOf("\\n", start);
    if (brk < 0) {
      wrapParagraph(text.substring(start));
      break;
    }
    wrapParagraph(text.substring(start, brk));
    start = brk + 2;
  }
}

void render() {
  display.setRotation(ROTATION);
  display.setFont(g_font);
  display.setTextColor(GxEPD_BLACK);
  display.setFullWindow();

  const int lineHeight = g_font->yAdvance;

  // y1 from getTextBounds is the offset from the baseline to the top of the
  // glyphs, and it is negative - so -y1 is the ascent. GFX positions text by
  // its baseline, not its top corner.
  int16_t x1, y1;
  uint16_t w, h;
  display.getTextBounds(g_lineCount ? g_lines[0] : "X", 0, 0, &x1, &y1, &w, &h);
  const int ascent = -y1;

  const int blockHeight = (g_lineCount - 1) * lineHeight + h;
  int top = (display.height() - blockHeight) / 2;
  if (top < 0) top = 0;

  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    for (int i = 0; i < g_lineCount; i++) {
      if (g_lines[i].length() == 0) continue;
      display.getTextBounds(g_lines[i], 0, 0, &x1, &y1, &w, &h);
      display.setCursor((display.width() - (int)w) / 2 - x1,
                        top + i * lineHeight + ascent);
      display.print(g_lines[i]);
    }
  } while (display.nextPage());

  // Cuts the panel's power rail. Without it the driver chip sits warm and the
  // display can degrade if it is left energised for long stretches.
  display.hibernate();
}

// The bitmap's set bits are white, so paint black and draw the bits on top.
void renderImage() {
  display.setRotation(ROTATION);
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_BLACK);
    display.drawBitmap(0, 0, g_image, display.width(), display.height(), GxEPD_WHITE);
  } while (display.nextPage());
  display.hibernate();
}

void receiveImage(String args) {
  args.trim();
  const int space = args.indexOf(' ');
  const int w = args.substring(0, space).toInt();
  const int h = args.substring(space + 1).toInt();

  display.setRotation(ROTATION);
  if (space < 0 || w != display.width() || h != display.height()) {
    Serial.print("ERR image must be ");
    Serial.print(display.width());
    Serial.print("x");
    Serial.println(display.height());
    return;
  }

  Serial.println("SEND");
  const size_t received = Serial.readBytes(g_image, IMAGE_BYTES);
  if (received != IMAGE_BYTES) {
    Serial.print("ERR image truncated at ");
    Serial.println(received);
    return;
  }
  renderImage();
  Serial.println("OK");
}

void clearScreen() {
  display.setRotation(ROTATION);
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
  } while (display.nextPage());
  display.hibernate();
}

void handleCommand(const String &raw) {
  String line = raw;
  line.trim();
  if (line.length() == 0) return;

  if (line.equalsIgnoreCase("PING")) {
    Serial.println("PONG");
    return;
  }

  if (line.equalsIgnoreCase("CLEAR")) {
    clearScreen();
    Serial.println("OK");
    return;
  }

  if (line.startsWith("FONT ")) {
    String size = line.substring(5);
    size.trim();
    size.toUpperCase();
    if (size == "S") {
      g_font = &FreeSansBold12pt7b;
    } else if (size == "M") {
      g_font = &FreeSansBold18pt7b;
    } else if (size == "L") {
      g_font = &FreeSansBold24pt7b;
    } else {
      Serial.println("ERR font must be S, M or L");
      return;
    }
    Serial.println("OK");
    return;
  }

  if (line.startsWith("TEXT ")) {
    display.setFont(g_font);  // wrapping measures against the active font
    layout(line.substring(5));
    render();
    Serial.println("OK");
    return;
  }

  if (line.startsWith("IMAGE ")) {
    receiveImage(line.substring(6));
    return;
  }

  Serial.print("ERR unknown command: ");
  Serial.println(line);
}

void setup() {
  // The default 256-byte RX buffer is too tight for a 48000-byte image burst.
  Serial.setRxBufferSize(4096);
  Serial.begin(115200);
  Serial.setTimeout(IMAGE_TIMEOUT_MS);

  // Passing 0 as the diagnostic bitrate keeps GxEPD2 from printing its own
  // chatter onto the same serial line the protocol runs over.
  display.init(0, true, 2, false);
  SPI.end();
  SPI.begin(EPD_SCK, EPD_MISO, EPD_MOSI, EPD_CS);

  Serial.println("READY");
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;

    if (c == '\n') {
      g_buffer[g_length] = '\0';
      if (g_truncated) {
        Serial.println("ERR command too long");
      } else {
        handleCommand(String(g_buffer));
      }
      g_length = 0;
      g_truncated = false;
      continue;
    }

    if (g_length < MAX_COMMAND - 1) {
      g_buffer[g_length++] = c;
    } else {
      g_truncated = true;
    }
  }
}
