# NAO audio bridge

Process that runs on the robot and streams the front microphone to one laptop over WebSocket port 8765. Audio is 16 kHz mono PCM. The first frame is a JSON format announcement, later frames are PCM, and a status JSON follows about every 5 seconds. Shapes live in [nao-websocket-bridge-protocol](https://github.com/icampus-wildau/nao-websocket-bridge-protocol), which the install copies next to `audio_bridge.py`. The laptop reader is [nao-websocket-bridge-client](https://github.com/icampus-wildau/nao-websocket-bridge-client).

The microphone is subscribed while a client is connected and released about two seconds after the last client leaves. The bridge keeps one client: a new connection from the same machine takes over, and a connection from another machine is closed.

This is a separate process. Do not add it under `[python]` in `autoload.ini`. That section runs inside NAOqi, and this program blocks in its own socket loop and opens its own `qi.Application`. The install registers `/home/nao/nao-audio-bridge/run_forever.sh` under `[program]` instead. `run_forever.sh` restarts the interpreter if Python exits. The process reconnects to NAOqi on its own. `ALServiceManager.startService` does not see this process.

## Install from a laptop

```bash
curl -LsSf https://raw.githubusercontent.com/icampus-wildau/nao-websocket-bridge/main/packaging/install.sh | sh
```

The script downloads the `main` branch, asks for the NAO IP and the `nao` password, copies the files, registers autoload, and starts the bridge.

## Install while logged in on the robot

```bash
# While being connected via ssh to the robot
curl -LsSf https://raw.githubusercontent.com/icampus-wildau/nao-websocket-bridge/main/packaging/install_local.sh | sh
```

The script writes into the current home directory and uses `python` and whichever of `wget` or `curl` is installed.

From a checkout already on the robot, `sh packaging/install_local.sh` installs those files instead of downloading `main`.

## Install a local checkout

Use this to copy files that are not on `main` yet:

```bash
sh packaging/install_on_nao.sh nao@ROBOT_IP
```

The protocol package comes from `../nao-websocket-bridge-protocol` when that directory exists, and otherwise from the `main` branch on GitHub. Set `PROTOCOL_SRC` to a checkout root to choose another copy.

## On the robot

Every method copies these into `/home/nao/nao-audio-bridge/`:

- `audio_bridge.py`
- `SimpleWebSocketServer/`
- `nao_websocket_bridge_protocol/` (beside `audio_bridge.py`, so Python 2.7 can import it)
- `run_forever.sh` and `register_autoload.py`

Autoload is `/home/nao/naoqi/preferences/autoload.ini`, under `[program]`. NAOqi merges that file with `/etc/naoqi/autoload.ini`. Leave the system file alone. The install starts the bridge immediately. The autoload entry starts it again on the next boot.

Stop it:

```bash
ssh nao@ROBOT_IP /home/nao/nao-audio-bridge/run_forever.sh stop
```

Follow the log:

```bash
ssh nao@ROBOT_IP tail -f /home/nao/nao-audio-bridge/audio_bridge.log
```

`audio_bridge.log` rotates at 1 MB with one backup. The restart loop writes `run_forever.log`.

## Token

To refuse other clients, create `/home/nao/nao-audio-bridge/bridge.env`:

```
NAO_BRIDGE_TOKEN=choose-a-secret
```

Restart with `run_forever.sh stop`, then `run_forever.sh`. Clients connect to `ws://ROBOT_IP:8765/choose-a-secret`. With no token, `ws://ROBOT_IP:8765` stays open.

## If preferences is not writable

`run_forever.sh` still restarts a dead interpreter. Start it at boot from cron:

```cron
@reboot /home/nao/nao-audio-bridge/run_forever.sh
```

## Run by hand

On the robot, with NAOqi's Python, from `/home/nao/nao-audio-bridge`:

```bash
python audio_bridge.py --port 8765 --token secret
```

`--host` defaults to `0.0.0.0`. `--qi-url` defaults to `tcp://127.0.0.1:9559`. `run_forever.sh` passes `--token` when `NAO_BRIDGE_TOKEN` is set in `bridge.env`.

Laptops install `nao-websocket-bridge-client`. This repository is the copy that runs on the robot.
