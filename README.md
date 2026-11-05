# nao-websocket-bridge

*Stream a NAO's front microphone to one laptop.*

![Python](https://img.shields.io/badge/python-2.7-blue?style=flat-square)
![CI](https://img.shields.io/github/actions/workflow/status/icampus-wildau/nao-websocket-bridge/ci.yml?style=flat-square&label=CI)
![License: Apache 2.0](https://img.shields.io/badge/license-Apache%20License%202.0-blue)

The process runs on the robot, in NAOqi's Python 2.7, and listens on WebSocket port 8765. Audio is 16 kHz mono PCM. The first frame is a JSON format announcement, later frames are PCM, and a status JSON follows about every 5 seconds. Shapes live in [nao-websocket-bridge-protocol](https://github.com/icampus-wildau/nao-websocket-bridge-protocol). The laptop reader is [nao-websocket-bridge-client](https://github.com/icampus-wildau/nao-websocket-bridge-client).

The microphone is subscribed while a client is connected and released about two seconds after the last client leaves. A new connection from the same machine takes over. A connection from another machine is closed.

Do not add this program under `[python]` in `autoload.ini`. That section runs inside NAOqi. The install registers `/home/nao/nao-audio-bridge/run_forever.sh` under `[program]`. `run_forever.sh` restarts the interpreter if Python exits and reconnects to NAOqi on its own.

## Install via the NAO Fair app

The NAO Fair app installs this process from its System tab. A robot without the bridge still accepts a NAOqi connection for speech, motion, and gesture tests. Conversations wait for the microphone stream; System reports bridge availability separately from the autostart assignment.

## Install from a laptop

Needs `curl` or `wget`, OpenSSH (`ssh` and `scp`), `gzip`, and `tar`, plus a route to the robot's SSH port and TCP 8765. On Windows, run this in WSL. It is not a PowerShell command.

```bash
curl -LsSf https://raw.githubusercontent.com/icampus-wildau/nao-websocket-bridge/main/packaging/install.sh | sh
```

The script downloads the latest GitHub release, asks for the NAO IP and the `nao` password, copies the files, registers autoload, and starts the bridge. The first connection prints the robot's SSH host key and waits for `yes`. The address is the IP on its own, for example `192.168.2.57`.

While logged in on the robot, the same install writes into the current home directory:

```bash
curl -LsSf https://raw.githubusercontent.com/icampus-wildau/nao-websocket-bridge/main/packaging/install_local.sh | sh
# from a checkout already on the robot, this uses those files instead of main:
sh packaging/install_local.sh
```

To copy a checkout that is not on `main` yet:

```bash
sh packaging/install_on_nao.sh nao@ROBOT_IP
```

Set `PROTOCOL_SRC` to a checkout root to select the protocol. Otherwise the installer uses `../nao-websocket-bridge-protocol` when that directory exists. The laptop installer falls back to the pinned PyPI package. The on-robot installer downloads `main` from GitHub.

## On the robot

Files land in `/home/nao/nao-audio-bridge/`: `audio_bridge.py`, `SimpleWebSocketServer/`, `nao_websocket_bridge_protocol/` (beside `audio_bridge.py`, so Python 2.7 can import it), `run_forever.sh`, and `register_autoload.py`.

Autoload is `/home/nao/naoqi/preferences/autoload.ini`, under `[program]`. NAOqi merges that file with `/etc/naoqi/autoload.ini`. Leave the system file alone. The install starts the bridge immediately. The autoload entry starts it again on the next boot.

```bash
ssh nao@ROBOT_IP /home/nao/nao-audio-bridge/run_forever.sh stop
ssh nao@ROBOT_IP tail -f /home/nao/nao-audio-bridge/audio_bridge.log
```

`audio_bridge.log` rotates at 1 MB with one backup. The restart loop writes `run_forever.log`.

## Token

Create `/home/nao/nao-audio-bridge/bridge.env` to refuse other clients:

```
NAO_BRIDGE_TOKEN=choose-a-secret
```

Restart with `run_forever.sh stop`, then `run_forever.sh`. Clients then use `ws://ROBOT_IP:8765/choose-a-secret`. With no token, `ws://ROBOT_IP:8765` stays open.

If `preferences` is not writable, `run_forever.sh` still restarts a dead interpreter. Start it at boot from cron:

```cron
@reboot /home/nao/nao-audio-bridge/run_forever.sh
```

## Run by hand

On the robot, with NAOqi's Python, from `/home/nao/nao-audio-bridge`:

```bash
python audio_bridge.py --port 8765 --token secret
# --host defaults to 0.0.0.0
# --qi-url defaults to tcp://127.0.0.1:9559
# run_forever.sh passes --token when NAO_BRIDGE_TOKEN is set in bridge.env
```

Laptops install `nao-websocket-bridge-client`. This repository is the copy that runs on the robot.
