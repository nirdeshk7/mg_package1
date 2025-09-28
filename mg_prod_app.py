import os
import json
import threading
import queue
import socket
from pathlib import Path
import yaml
import streamlit as st

# Optional imports: Vosk and paho-mqtt
try:
    from vosk import Model, KaldiRecognizer
    VOSK_AVAILABLE = True
except Exception:
    Model = None
    KaldiRecognizer = None
    VOSK_AVAILABLE = False

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except Exception:
    mqtt = None
    MQTT_AVAILABLE = False

def safe_base_path():
    try:
        return Path(__file__).parent
    except Exception:
        return Path.cwd()

BASE = safe_base_path()
CFG_PATH = BASE / "config.yaml"
DEVICES_PATH = BASE / "devices.json"

if CFG_PATH.exists():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
else:
    cfg = {}

APP_NAME = cfg.get("app_name", "MG")
MODEL_PATH_DEFAULT = cfg.get("vosk_model_path", str(BASE / "model"))
LOCAL_ONLY_DEFAULT = cfg.get("local_only", True)
MQTT_BROKER_DEFAULT = cfg.get("mqtt_broker", "mqtt://localhost:1883")

st.set_page_config(page_title=APP_NAME, layout="wide")
st.title(f"{APP_NAME} - Hybrid Smart Home Assistant")

if DEVICES_PATH.exists():
    try:
        with open(DEVICES_PATH, "r", encoding="utf-8") as f:
            devices_file = json.load(f)
            devices = devices_file.get("devices", [])
    except Exception:
        devices = []
else:
    devices = []

def check_internet(timeout=1.0):
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=timeout)
        return True
    except OSError:
        return False

def online_text_processing(text):
    # Replace with actual online AI API call
    return f"Online AI reply: {text}"

def process_text(text):
    if check_internet():
        return online_text_processing(text)
    else:
        return "Offline response: " + text

from urllib.parse import urlparse

class MQTTAdapter:
    def __init__(self, broker_url=MQTT_BROKER_DEFAULT, client_id=None):
        self.broker_url = broker_url
        self.client = None
        self.connected = False
        self.client_id = client_id
        if MQTT_AVAILABLE:
            self.client = mqtt.Client(client_id)
            self.client.on_connect = self.on_connect
            self.client.on_message = self.on_message

    def connect(self):
        if not MQTT_AVAILABLE:
            st.warning("paho-mqtt not installed, MQTT disabled.")
            return False

        parsed = urlparse(self.broker_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (8883 if parsed.scheme in ("mqtts", "ssl", "tls") else 1883)

        try:
            if parsed.scheme in ("mqtts", "ssl", "tls"):
                self.client.tls_set()
            self.client.connect(host, port)
            self.client.loop_start()
            return True
        except Exception as e:
            st.error(f"MQTT connection failed: {e}")
            return False

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            st.sidebar.success("Connected to MQTT Broker")
            try:
                client.subscribe("home/devices/#")
            except Exception:
                pass
        else:
            st.sidebar.error(f"MQTT connect failed with code {rc}")

    def on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode()
        except Exception:
            payload = str(msg.payload)
        if "mqtt_messages" not in st.session_state:
            st.session_state.mqtt_messages = []
        st.session_state.mqtt_messages.append({"topic": msg.topic, "payload": payload})

    def publish(self, topic, message):
        if self.client and self.connected:
            try:
                self.client.publish(topic, message)
                return True
            except Exception as e:
                st.warning(f"Publish error: {e}")
                return False
        else:
            st.warning("MQTT not connected; cannot publish.")
            return False

    def disconnect(self):
        if self.client:
            try:
                self.client.loop_stop(force=True)
                self.client.disconnect()
            except Exception:
                pass
        self.connected = False

class OfflineSpeechRecognizer:
    def __init__(self, model_path: str = MODEL_PATH_DEFAULT, sample_rate: int = 16000):
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.model = None
        self.recognizer = None
        self.audio_thread = None
        self.q = queue.Queue()
        self.running = False

    def start(self):
        if not VOSK_AVAILABLE:
            st.warning("Vosk not installed. Install vosk and pyaudio to use offline mode.")
            return False

        if not Path(self.model_path).exists():
            st.error(f"Vosk model missing at '{self.model_path}'. Please download a model and set 'vosk_model_path' in config.yaml.")
            return False

        try:
            self.model = Model(self.model_path)
            self.recognizer = KaldiRecognizer(self.model, self.sample_rate)
        except Exception as e:
            st.error(f"Failed to initialize Vosk model: {e}")
            return False

        self.running = True
        self.audio_thread = threading.Thread(target=self._listen_audio, daemon=True)
        self.audio_thread.start()
        return True

    def _listen_audio(self):
        try:
            import pyaudio
        except Exception as e:
            self.q.put(json.dumps({"error": f"pyaudio import failed: {e}"}))
            self.running = False
            return

        p = pyaudio.PyAudio()
        try:
            stream = p.open(format=pyaudio.paInt16, channels=1,
                            rate=self.sample_rate, input=True, frames_per_buffer=8000)
        except Exception as e:
            self.q.put(json.dumps({"error": f"Failed to open microphone: {e}"}))
            self.running = False
            p.terminate()
            return

        stream.start_stream()
        try:
            while self.running:
                data = stream.read(4000, exception_on_overflow=False)
                if self.recognizer.AcceptWaveform(data):
                    res = self.recognizer.Result()
                    self.q.put(res)
        except Exception as e:
            self.q.put(json.dumps({"error": f"Audio thread error: {e}"}))
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            p.terminate()

    def get_result(self):
        try:
            if not self.q.empty():
                raw = self.q.get_nowait()
                try:
                    parsed = json.loads(raw)
                    if 'text' in parsed:
                        return parsed.get('text', '')
                    elif 'error' in parsed:
                        return f"[ERROR] {parsed['error']}"
                    else:
                        return raw
                except Exception:
                    return raw
        except queue.Empty:
            return None

    def stop(self):
        self.running = False
        if self.audio_thread and self.audio_thread.is_alive():
            self.audio_thread.join(timeout=2.0)

if "recognizer" not in st.session_state:
    st.session_state.recognizer = None
if "transcript" not in st.session_state:
    st.session_state.transcript = ""
if "recognizer_running" not in st.session_state:
    st.session_state.recognizer_running = False
if "mqtt_adapter" not in st.session_state:
    st.session_state.mqtt_adapter = None
if "mqtt_messages" not in st.session_state:
    st.session_state.mqtt_messages = []

col1, col2 = st.columns([1, 2])

with col1:
    uploaded = st.file_uploader("Upload recordings/documents (txt/pdf/image)", accept_multiple_files=True)
    if uploaded:
        for file in uploaded:
            st.write(f"- **{file.name}** ({file.size} bytes)")
            if file.type.startswith("image/"):
                st.image(file, caption=file.name, use_column_width=True)

with col1:
    st.markdown("---")
    st.write("**Offline settings**")
    model_path_input = st.text_input("Vosk model path", value=str(MODEL_PATH_DEFAULT))
    local_only_toggle = st.checkbox("Prefer offline (local_only)", value=LOCAL_ONLY_DEFAULT)
    mqtt_broker_input = st.text_input("MQTT broker URL", value=str(MQTT_BROKER_DEFAULT))

    if st.button("Start Offline Recognizer"):
        if st.session_state.recognizer_running:
            st.info("Recognizer already running.")
        else:
            st.session_state.recognizer = OfflineSpeechRecognizer(model_path=model_path_input)
            ok = st.session_state.recognizer.start()
            if ok:
                st.session_state.recognizer_running = True
                st.success("Offline recognizer started.")
            else:
                st.session_state.recognizer = None
                st.session_state.recognizer_running = False

        if st.button("Stop Offline Recognizer"):
            if st.session_state.recognizer:
                st.session_state.recognizer.stop()
            st.session_state.recognizer = None
            st.session_state.recognizer_running = False
            st.success("Recognizer stopped.")

    st.markdown("---")
    st.write("**MQTT**")
    if st.button("Connect MQTT"):
        if not MQTT_AVAILABLE:
            st.warning("paho-mqtt not installed; cannot connect.")
        else:
            if st.session_state.mqtt_adapter and st.session_state.mqtt_adapter.connected:
                st.info("Already connected to MQTT broker.")
            else:
                st.session_state.mqtt_adapter = MQTTAdapter(broker_url=mqtt_broker_input)
                ok = st.session_state.mqtt_adapter.connect()
                if ok:
                    st.success("MQTT adapter started.")

    if st.button("Disconnect MQTT"):
        if st.session_state.mqtt_adapter:
            st.session_state.mqtt_adapter.disconnect()
        st.session_state.mqtt_adapter = None
        st.success("MQTT disconnected.")

with col2:
    st.subheader("Talk to MG or type command")
    user_text = st.text_input("Text input (press Enter to send):", value="")
    if st.button("Send Text"):
        if user_text.strip():
            if local_only_toggle and not check_internet():
                reply = "Offline response: " + user_text
            else:
                reply = process_text(user_text)
            st.text_area("MG Replies:", value=reply, height=200)

    st.markdown("### Live transcript (offline recognizer)")
    transcript_box = st.empty()
    if st.session_state.get("recognizer") and st.session_state.get("recognizer_running"):
        res = st.session_state.recognizer.get_result()
        if res:
            st.session_state.transcript += (" " + res).strip()
    transcript_box.text_area("Transcript", value=st.session_state.transcript, height=200)

    st.markdown("### Devices")
    if devices:
        for dev in devices:
            st.write(f"- **{dev.get('name')}** ({dev.get('type')}) — topic: `{dev.get('topic')}`")
            if st.button(f"Toggle {dev.get('name')}", key=f"toggle_{dev.get('name')}"):
                payload = json.dumps({"cmd": "toggle", "device": dev.get('name')})
                if st.session_state.get("mqtt_adapter") and st.session_state.mqtt_adapter.connected:
                    ok = st.session_state.mqtt_adapter.publish(dev.get('topic'), payload)
                    if ok:
                        st.success(f"Sent toggle to {dev.get('name')}")
                else:
                    st.warning("MQTT not connected; cannot send command.")

    st.markdown("### MQTT Messages (incoming)")
    if st.session_state.mqtt_messages:
        for m in st.session_state.mqtt_messages[-20:]:
            st.write(f"- {m['topic']}: {m['payload']}")

st.sidebar.title("Status")
st.sidebar.write(f"- Vosk installed: {VOSK_AVAILABLE}")
st.sidebar.write(f"- paho-mqtt installed: {MQTT_AVAILABLE}")
st.sidebar.write(f"- Internet reachable: {check_internet()}")
st.sidebar.write(f"- Recognizer running: {st.session_state.get('recognizer_running', False)}")
st.sidebar.write(f"- MQTT connected: {st.session_state.get('mqtt_adapter') is not None and st.session_state.get('mqtt_adapter').connected}")
st.sidebar.write(f"- Devices loaded: {len(devices)}")
