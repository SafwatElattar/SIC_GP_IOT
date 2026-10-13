#include <WiFi.h>
#include <PubSubClient.h>
#include <DHT.h>
#include <ArduinoJson.h>

// --- Pin Definitions ---
#define DHTPIN        15
#define DHTTYPE       DHT22
#define POT_BPM_PIN   34
#define POT_SPO2_PIN  35
#define SW_WEAR_PIN   13

// --- Network & MQTT Settings ---
const char* ssid        = "Wokwi-GUEST";
const char* password    = "";
const char* mqtt_server = "broker.hivemq.com";
const int   mqtt_port   = 1883;

// Topic monitored by PulseCare dashboard
const char* mqtt_topic  = "pulsecare/iot/current";

WiFiClient espClient;
PubSubClient client(espClient);
DHT dht(DHTPIN, DHTTYPE);

unsigned long lastPublish = 0;
const unsigned long interval = 2000; // Publish every 2 seconds

void setup_wifi() {
  delay(10);
  Serial.println();
  Serial.print("Connecting to WiFi: ");
  Serial.println(ssid);

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\nWiFi connected! IP address: ");
  Serial.println(WiFi.localIP());
}

void reconnect() {
  while (!client.connected()) {
    Serial.print("Attempting MQTT connection...");
    String clientId = "ESP32PulseCare-" + String(random(0xffff), HEX);
    
    if (client.connect(clientId.c_str())) {
      Serial.println("connected!");
    } else {
      Serial.print("failed, rc=");
      Serial.print(client.state());
      Serial.println(" retrying in 3 seconds...");
      delay(3000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(SW_WEAR_PIN, INPUT_PULLDOWN);

  dht.begin();
  setup_wifi();
  
  client.setServer(mqtt_server, mqtt_port);
}

void loop() {
  if (!client.connected()) {
    reconnect();
  }
  client.loop();

  unsigned long now = millis();
  if (now - lastPublish >= interval) {
    lastPublish = now;

    // 1. Read DHT22
    float temperature = dht.readTemperature();
    if (isnan(temperature)) {
      temperature = 36.8; // Fallback sensible default if sensor glitches
    }

    // 2. Read Potentiometers (ESP32 ADC: 0 - 4095)
    int rawBPM  = analogRead(POT_BPM_PIN);
    int rawSPO2 = analogRead(POT_SPO2_PIN);

    int bpm  = map(rawBPM, 0, 4095, 45, 175);
    int spo2 = map(rawSPO2, 0, 4095, 75, 100);

    // 3. Read Wear Status Switch
    bool is_wearing = digitalRead(SW_WEAR_PIN) == HIGH;

    // When the wearable is removed, zero-out vitals
    if (!is_wearing) {
      bpm = 0;
      spo2 = 0;
    }

    // 4. Construct JSON Payload conforming to PulseCare schema
    StaticJsonDocument<256> doc;
    doc["heart_rate"]  = bpm;
    doc["temperature"] = serialized(String(temperature, 1));
    doc["spo2"]        = spo2;
    doc["is_wearing"]   = is_wearing;
    doc["timestamp"]    = millis(); // Or epoch timestamp if NTP is synced

    char buffer[256];
    serializeJson(doc, buffer);

    Serial.print("Publishing to MQTT: ");
    Serial.println(buffer);
    client.publish(mqtt_topic, buffer);
  }
}