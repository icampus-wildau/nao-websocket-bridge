# NAO audio bridge

The bridge runs on the robot and sends realtime streams to the laptop over a WebSocket on port 8765. The first stream is the front microphone: 16 kHz PCM. Another NAOqi device implements the same `BridgeSource` slot (`start`, `stop`, enqueue bytes) without a second socket loop. Message shapes live in `nao_websocket_bridge_protocol`, which the install script copies next to `audio_bridge.py`.

It is a separate process. Do not add it under `[python]` in `autoload.ini`: that runs inside NAOqi, and this program blocks in its own socket loop and opens its own `qi.Application`.

The process reconnects to NAOqi on its own. `run_forever.sh` only restarts it if the interpreter itself exits. The microphone is subscribed while a laptop is connected, and released about two seconds after the last client leaves. A new connection replaces the previous one.

## Install

The copy commands, the file list, and autostart are in the repository [README](../README.md#copy-the-audio-bridge-to-the-nao). From this directory the same copy is:

```bash
chmod +x packaging/install_on_nao.sh packaging/run_forever.sh
./packaging/install_on_nao.sh nao@ROBOT_IP
```

The script copies `audio_bridge.py`, `SimpleWebSocketServer/`, and the `nao_websocket_bridge_protocol` package into `/home/nao/nao-audio-bridge/`. It uses a checkout at `../nao-websocket-bridge-protocol` when that package is already there. Otherwise it clones [nao-websocket-bridge-protocol](https://github.com/icampus-wildau/nao-websocket-bridge-protocol) and copies the package from the clone. Set `PROTOCOL_SRC` to a checkout root to choose another local copy. On the robot the package sits beside `audio_bridge.py` so Python 2.7 can import it.

The install script registers `/home/nao/nao-audio-bridge/run_forever.sh` under `[program]` in `/home/nao/naoqi/preferences/autoload.ini` and starts the bridge immediately. `ALServiceManager.startService` does not see this process. A copied package manifest is not an installed service.

```bash
ssh nao@ROBOT_IP /home/nao/nao-audio-bridge/run_forever.sh stop
```

Follow the log:

```bash
ssh nao@ROBOT_IP tail -f /home/nao/nao-audio-bridge/audio_bridge.log
```

Logs go to `/home/nao/nao-audio-bridge/audio_bridge.log` (1 MB, one backup) and to `run_forever.log`.

## Optional token

To refuse other clients on a shared network, create `/home/nao/nao-audio-bridge/bridge.env` on the robot:

```bash
NAO_BRIDGE_TOKEN=choose-a-secret
```

Restart the service. The laptop reads the same variable (`NAO_BRIDGE_TOKEN` in the environment or `nao_bridge_token` in `config.toml`) and connects to `ws://ROBOT_IP:8765/choose-a-secret`. With no token, the open URL `ws://ROBOT_IP:8765` still works.

## Crontab fallback

Use this only when `~/naoqi/preferences` is not writable. It starts the bridge at boot, and `run_forever.sh` still restarts a dead interpreter:

```cron
@reboot /home/nao/nao-audio-bridge/run_forever.sh
```
