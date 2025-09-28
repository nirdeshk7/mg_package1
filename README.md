# MG Smart Home Assistant

## Quick start

1. Create a Python virtual environment and activate it.
2. Install dependencies: `pip install -r requirements.txt` (special note about pyaudio below).
3. Download a Vosk speech model (small or medium) and place it in the 'model' folder or update config.yaml.
4. Modify devices.json and config.yaml as needed to reflect your setup.
5. Run the app: `streamlit run mg_prod_app.py`.

## Notes

- Pyaudio can be tricky to install; on Windows, use prebuilt wheels or pipwin; on Linux, install portaudio dev packages before pip install pyaudio.
- Consider client-side microphone capture with `streamlit-webrtc` for cloud/remote deployment.
- Offline mode (Vosk) can be disabled by setting `local_only` to false in config.yaml.

