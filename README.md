# Project Gator

Project Gator is a local-first research and reasoning stack built around a native inference kernel, a structural blueprint map, and a self-maintaining learning loop for constrained GPU hardware.

## Components

- Native kernel: [src/inference/gator_kern.cpp](src/inference/gator_kern.cpp)
- Kernel runtime wrapper: [src/inference/gator_kern.py](src/inference/gator_kern.py)
- Structural blueprint: [src/core/gator_map.py](src/core/gator_map.py)
- Maintenance loop: [src/maintenance.py](src/maintenance.py)
- Research store: [src/scholar_sense.py](src/scholar_sense.py)
- Runtime bridge: [src/gator_bridge.py](src/gator_bridge.py)
- Web UI: [src/interfaces/webui.py](src/interfaces/webui.py)

## One-Line Install

```bash
git clone <your-repo-url> Gator && cd Gator && bash install.sh
```

Telegram-enabled one-line installer:

```bash
git clone <your-repo-url> Gator && cd Gator && GATOR_TG_BOT_TOKEN="<token>" GATOR_TG_AUTH_CHAT_ID="<chat_id>" bash install.sh
```

## Start

```bash
GATOR_DAEMON=true ./wakeup
```

## Release Gates

Run the production gauntlet with:

```bash
./venv/bin/python tests/test_release_gauntlet.py
```

Run the swarm gauntlet with:

```bash
./venv/bin/python tests/test_swarm_gauntlet.py
```

Hive map output is written to `bin/gator_map/gator_hive_map.json`.
