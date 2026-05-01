import time
import json
import paho.mqtt.client as mqtt # pyright: ignore[reportMissingImports]
import datetime
import requests
from abc import ABC, abstractmethod
from zoneinfo import ZoneInfo

# Configuration
BROKER = "192.168.0.234"
PORT = 32771
TOPIC = "leds/control"
TZ = ZoneInfo("America/New_York")

class LEDModule(ABC):
    """Base class for all LED features."""
    def __init__(self, interval_seconds):
        self.interval_seconds = interval_seconds
        self.last_updated = 0

    def should_update(self, current_time):
        """Returns True if the module is due for an update."""
        return (current_time - self.last_updated) >= self.interval_seconds

    @abstractmethod
    def get_update(self, now):
        """Returns a dict of {led_index: [R, G, B]} for the current state."""
        pass

class BinaryClock(LEDModule):
    """Displays time in binary format on specific LEDs."""
    def __init__(self, interval_seconds=1):
        # We check every second, but only return updates when the minute changes
        super().__init__(interval_seconds)
        self.last_minute = -1

    def should_update(self, current_time):
        # Always check to see if the minute has ticked over
        now = datetime.datetime.now(TZ)
        if now.minute != self.last_minute:
            return True
        return False

    def encode_binary(self, value, bits):
        binary_str = bin(value)[2:].zfill(bits)
        return [int(bit) for bit in binary_str]

    def get_update(self, now):
        self.last_minute = now.minute
        self.last_updated = time.time()
        
        # Color definitions
        timeBitColor = lambda bit: [0, 255, 0] if bit == 1 else [23, 66, 80]
        amPmBitColor = lambda bit: [255, 255, 0] if bit == 0 else [128, 0, 128]

        update = {}
        
        # AM/PM bit -> LED 27
        # 1 if PM, 0 if AM
        is_pm = 1 if now.hour >= 12 else 0
        update["27"] = amPmBitColor(is_pm)
        
        # Hours -> LEDs 26 down to 22 (5 bits)
        display_hour = 12 if now.hour % 12 == 0 else now.hour % 12
        for i, bit in enumerate(self.encode_binary(display_hour, 5)):
            update[f"{26-i}"] = timeBitColor(bit)
            
        # Minutes -> LEDs 16 up to 21 (6 bits)
        for i, bit in enumerate(self.encode_binary(now.minute, 6)):
            update[f"{16+i}"] = timeBitColor(bit)
            
        return update

class Blinky(LEDModule):
    """Simple module that blinks a specific LED on and off."""
    def __init__(self, interval_seconds=1):
        super().__init__(interval_seconds)
        self.curSpot = 30
        self.velocity = 1
        self.secondColor = False
    
    def get_update(self, now):
        self.last_updated = time.time()
        # payload = {str(self.curSpot): [0, 0, 0]}
        payload = {}
        
        # Move first, then check if we need to flip velocity for the next turn
        self.curSpot = self.curSpot - 1 if self.velocity == 1 else self.curSpot + 1
        self.velocity = self.velocity * -1 if self.curSpot != 29 else self.velocity
        payload[str(self.curSpot)] = [255, 0, 0] if not self.secondColor else [0, 0, 255]
        self.secondColor = True
        return payload

class WeatherModule(LEDModule):
    """Example module for showing weather data."""
    def __init__(self, interval_seconds=60): # Update every 10 minutes
        super().__init__(interval_seconds)
    
    def get_update(self, now):
        self.last_updated = time.time()
        
        try:
            resp = requests.get("https://api.open-meteo.com/v1/forecast?latitude=38.9807&longitude=-76.9369&hourly=precipitation_probability,precipitation&timezone=America%2FNew_York&forecast_days=2&wind_speed_unit=mph&temperature_unit=fahrenheit&precipitation_unit=inch")
            resp_json = resp.json()
            date_string = datetime.datetime.now(TZ).strftime("%Y-%m-%dT%H:%M")
            if date_string in resp_json["hourly"]["time"]:
                start_index = (resp_json["hourly"]["time"]).index(date_string)
                prob_res = resp_json["hourly"]["precipitation_probability"][start_index:start_index+12]
                percep_res = resp_json["hourly"]["precipitation"][start_index:start_index+12]
                [(percep_res[i], prob_res[i]) for i in range(12)]
                payload = {}
                for i, index in enumerate([i for i in range(15, 3, -1)]):
                    val = round(prob_res[i] // 10 * 25.5)
                    payload[str(index)] = [val, 0, 25 if val == 0 else 0]
                
                return payload

        except:
            print("error happened")
    
        return {} # Returning empty dict as it's a placeholder

class AmbientBrightness(LEDModule):
    """Subscribes to leds/lux and adjusts brightness based on ambient light."""

    TOPIC_LUX        = "leds/lux"
    TOPIC_BRIGHTNESS = "leds/brightness"
    LUX_MIN          = 10      # → brightness 1.0
    LUX_MAX          = 1000    # → brightness 0.05
    BRIGHTNESS_MAX   = 1.0
    BRIGHTNESS_MIN   = 0.05

    def __init__(self, interval_seconds=1):
        super().__init__(interval_seconds)
        self._latest_lux = None

    def on_lux_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            self._latest_lux = float(payload["lux"])
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[AmbientBrightness] Bad lux payload: {e}")

    def register(self, mqtt_client):
        """Call after MQTT connects to subscribe to the lux topic."""
        mqtt_client.subscribe(self.TOPIC_LUX)
        mqtt_client.message_callback_add(self.TOPIC_LUX, self.on_lux_message)

    def _lux_to_brightness(self, lux: float) -> float:
        if lux > 30:
            return 0.9
        if lux > 8:
            return 0.5
        return 0.15

    def get_update(self, now):
        self.last_updated = time.time()
        return {}  # This module publishes directly; no LED index updates needed

class LEDController:
    """Manages the MQTT connection and module updates."""
    def __init__(self, broker, port, topic):
        self.client = mqtt.Client()
        self.broker = broker
        self.port = port
        self.topic = topic
        self.modules = []

    def add_module(self, module):
        self.modules.append(module)

    def connect(self):
        try:
            self.client.connect(self.broker, self.port)
            # Register any modules that need MQTT subscriptions
            for module in self.modules:
                if hasattr(module, "register"):
                    module.register(self.client)
            self.client.loop_start()
            print(f"Connected to MQTT broker at {self.broker}:{self.port}")
        except Exception as e:
            print(f"Failed to connect to MQTT broker: {e}")

    def run(self):
        self.connect()
        print(f"Starting main loop (Timezone: {TZ})...")
        try:
            while True:
                now_dt = datetime.datetime.now(TZ)
                now_ts = time.time()
                
                full_update = {}
                for module in self.modules:
                    if module.should_update(now_ts):
                        update = module.get_update(now_dt)
                        # Handle AmbientBrightness publishing directly to brightness topic
                        if hasattr(module, "_latest_lux") and module._latest_lux is not None:
                            brightness = module._lux_to_brightness(module._latest_lux)
                            self.client.publish(AmbientBrightness.TOPIC_BRIGHTNESS, str(brightness))
                            print(f"[{now_dt.strftime('%H:%M:%S')}] Brightness → {brightness} (lux={module._latest_lux:.1f})")
                        if update:
                            full_update.update(update)
                
                if full_update:
                    self.client.publish(self.topic, json.dumps(full_update))
                    print(f"[{now_dt.strftime('%H:%M:%S')}] Sent update: {list(full_update.keys())}")
                
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down...")
            self.client.loop_stop()
            self.client.disconnect()

if __name__ == "__main__":
    controller = LEDController(BROKER, PORT, TOPIC)
    
    # Register modules
    controller.add_module(BinaryClock())
    # controller.add_module(WeatherModule())
    controller.add_module(Blinky())
    controller.add_module(AmbientBrightness())
    
    controller.run()