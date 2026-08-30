# SETUP — getting CivRealm + this MCP server running

These are the **exact steps that worked** on this machine, including the non-obvious fixes.
Verified environment: **macOS (Apple Silicon, arm64)**, Docker 29.4, `uv` 0.11, June 2026.

> TL;DR of the gotchas (details below):
> 1. Use **Python 3.11**, not 3.12+/3.14 — CivRealm pins (`gymnasium==0.29.1`, `ray==2.6.3`, …).
> 2. Install **`setuptools<81`** — `ray==2.6.3` imports the removed `pkg_resources`.
> 3. The freeciv-web image is **amd64-only** → runs under emulation on Apple Silicon (works, slower).
> 4. Publish **only host port `8080`** for the container — the default port map collides with macOS
>    Control Center (AirPlay Receiver) on port **7000**, and the client only ever dials `8080`.
> 5. **No Firefox/geckodriver needed** for headless play (Selenium is only for screenshots).

---

## 0. Prerequisites

- **Docker Desktop** running (`docker ps` works). CivRealm needs Docker ≥ 24.0.6.
- **`uv`** (or conda) to create a Python 3.11 env. (System Python 3.12+/3.14 will NOT work — see below.)
- Git.

```bash
docker --version          # Docker version 29.4.0  (≥24.0.6)
uv --version              # uv 0.11.x
```

## 1. Clone CivRealm (for source + console scripts)

The CivRealm checkout and this repo sit side by side in one workspace directory. The rest of this
guide calls it `$WORKSPACE`; point it wherever you keep your repos. This repo is assumed to be
cloned at `$WORKSPACE/civrealm-mcp-server`.

```bash
export WORKSPACE=/path/to/workspace       # e.g. export WORKSPACE="$HOME/Repos"
mkdir -p "$WORKSPACE" && cd "$WORKSPACE"
git clone --depth 1 https://github.com/bigai-ai/civrealm.git
# (optional, for reference) git clone --depth 1 https://github.com/bigai-ai/civrealm-llm-baseline.git
```

## 2. Create a Python 3.11 venv and install CivRealm

CivRealm pins old deps (`selenium==4.9.1`, `tornado==6.3.2`, `gymnasium==0.29.1`,
`ray==2.6.3; python_version<"3.12"`). These resolve cleanly on **3.11** but not on 3.12+/3.14.

```bash
cd "$WORKSPACE/civrealm-mcp-server"
uv venv --python 3.11 .venv
uv pip install --python .venv -e "$WORKSPACE/civrealm"    # or: uv pip install civrealm==0.1.2
```

### FIX: `ModuleNotFoundError: No module named 'pkg_resources'`

`ray==2.6.3` imports the deprecated `pkg_resources` at import time, and modern setuptools
(≥81) removed it. Install an older setuptools:

```bash
uv pip install --python .venv 'setuptools<81'
```

Verify the import chain:

```bash
.venv/bin/python -c "import civrealm, gymnasium, selenium, tornado; \
import gymnasium; print([e for e in gymnasium.envs.registry if 'civrealm' in e])"
# -> ['civrealm/FreecivBase-v0', 'civrealm/FreecivTensor-v0', ... 'civrealm/FreecivLLM-v0', ...]
```

> **About Selenium/Firefox:** `selenium` is a hard pip dependency and is imported at load,
> but a real browser (`Firefox` + `geckodriver`) is launched **only** when
> `debug.take_screenshot` or `debug.get_webpage_image` is enabled (both off by default).
> Headless programmatic play connects over a **WebSocket** only — **no geckodriver required**.
> (Confirmed: `civ_controller.py:142-144` creates the Selenium `CivMonitor` only `if self.visualize`.)

## 3. Bring up the freeciv-web Docker server

Pull CivRealm's customized image (≈1.5 GB download, ~13 GB on disk) and retag it. The bundled
console script does both:

```bash
.venv/bin/download_freeciv_web_image
# == docker pull civrealm/freeciv-web:latest ; docker tag civrealm/freeciv-web:latest freeciv/freeciv-web:latest
```

The image is **linux/amd64 only**; on Apple Silicon Docker runs it under emulation (a
`platform ... does not match` warning is expected and harmless — it works, just slower).

### FIX: do NOT use `start_freeciv_web_service` unchanged on macOS

That console script publishes host ports `7000-7009`, and **macOS Control Center / AirPlay
Receiver already listens on port 7000** → `docker run` fails with
`bind: address already in use`. The CivRealm Python client only ever connects to
`ws://localhost:8080/civsocket/<1000+client_port>` — i.e. **only host port 8080 is needed**
(the 6000-6009/7000-7009 ports are internal to the container). So start the container
publishing just 8080:

```bash
docker run -d --name freeciv-web --rm -p 8080:80 freeciv/freeciv-web:latest
```

(Alternative: turn off **System Settings → General → AirDrop & Handoff → AirPlay Receiver**
to free port 7000, then `.venv/bin/start_freeciv_web_service` works as documented.)

Verify it is serving (the entrypoint starts tomcat/proxy/civserver in the background, then
`sleep infinity` keeps the container alive — startup under emulation takes a few seconds):

```bash
docker ps                          # shows: freeciv-web ... 0.0.0.0:8080->80/tcp
curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:8080/   # -> 200
```

## 4. Smoke-test CivRealm itself

```bash
# Single player vs built-in AIs (bounded to 2 turns so it terminates quickly):
.venv/bin/test_civrealm --max_turns=2
# -> Reset with port: 6001 / Step: 0 ... action: ('unit', 101, 'build_city') / ... / Truncated: True
```

Two-player (one game, two terminals — both on the SAME client_port, first with `--minp=2`):

```bash
# terminal 1
.venv/bin/test_civrealm --minp=2 --username=myagent  --client_port=6001 --max_turns=3 --begin_turn_timeout=180
# terminal 2 (start within ~minute; both join civsocket 6001 / one game)
.venv/bin/test_civrealm        --username=myagent1 --client_port=6001 --max_turns=3 --begin_turn_timeout=180
```

(They are provably in one game: each client's log under `civrealm/logs/<date>_<username>.log`
shows the *other* player connecting, and both print an identical final `game results` dict.)

## 5. Install and run THIS MCP server

```bash
cd "$WORKSPACE/civrealm-mcp-server"              # re-export WORKSPACE first if this is a fresh shell
uv pip install --python .venv --no-deps -e .     # deps (civrealm, mcp, setuptools<81) already installed

# Run the stdio server directly (it waits for an MCP client on stdin/stdout):
.venv/bin/civrealm-mcp-server
#   or:  .venv/bin/python -m civrealm_mcp.server
```

### Acceptance check (drives a game purely through MCP tools)

Needs the Docker server above. It prints the state at each step; it asserts nothing, which is why
it is a script rather than a pytest test:

```bash
.venv/bin/python scripts/acceptance_check.py
```

### Offline test suites (no Docker, no model, no network, no API key)

These need none of the setup above — not even CivRealm:

```bash
pip install --no-deps -e . && pip install pytest
pytest        # 15 tests
```

### Wiring into an MCP client (e.g. Claude Desktop / Claude Code)

Use the **absolute** path to your own checkout (`echo "$WORKSPACE/civrealm-mcp-server"` prints it).
MCP client config is plain JSON: it does not expand `~` or `$WORKSPACE`.

```json
{
  "mcpServers": {
    "civrealm": {
      "command": "/path/to/civrealm-mcp-server/.venv/bin/civrealm-mcp-server"
    }
  }
}
```

The freeciv-web Docker container must be running (step 3) before calling `start_game`.

---

## Teardown

```bash
docker stop freeciv-web        # container uses --rm, so this also removes it
```

## Versions that worked (pinned reality)

```
macOS arm64 · Docker 29.4.0 · Python 3.11.15 (uv) · civrealm 0.1.2 · gymnasium 0.29.1
selenium 4.9.1 · tornado 6.3.2 · ray 2.6.3 · numpy 2.4.6 · setuptools 80.10.2 · mcp 1.27.2
freeciv-web image: civrealm/freeciv-web:latest (digest sha256:4923253876a7, linux/amd64)
```

## Local-LLM contestant for the play loop (Ollama — cross-platform, zero API usage)

The single-agent play loop's contestant is pluggable: a `claude` subagent (`--backend claude`,
default), the Anthropic Messages API (`--backend api`, below), or a small **local model via
Ollama** (`--backend local`). Ollama wraps llama.cpp/GGUF, so it runs on macOS / Linux / Windows
(no API key, no usage).

Install Ollama (cross-platform):

```bash
curl -fsSL https://ollama.com/install.sh | sh     # recommended on Linux AND macOS
# macOS alternatives: `brew install ollama` (CLI only) · `brew install --cask ollama` (GUI app)
#                     · or the app from https://ollama.com/download
```

Start the server (the app/service does this automatically) and pull the model:

```bash
ollama serve &                  # if not already running as a service
ollama pull llama3.2:1b         # Llama-3.2-1B-Instruct, ~1.3 GB GGUF
```

Run the play loop against it (constrained mode; on this backend the per-turn schema goes into
Ollama's `format`, so llama.cpp compiles it to a grammar and an out-of-enum option cannot be
sampled):

```bash
.venv/bin/python -m playloop.loop --backend local --model llama3.2:1b --max-turns 50
# default --model for --backend local is llama3.2:1b; set OLLAMA_HOST if not localhost:11434
```

**Acceleration note (verified on Apple M3):** Ollama runs GGUF text models like `llama3.2:1b`
on the **llama.cpp/GGML Metal** backend (GPU-accelerated — "offloaded 17/17 layers to GPU"),
**not** MLX. `OLLAMA_NEW_ENGINE=1` does not switch a GGUF text model to MLX; this build's MLX
(`mlx_metal_v3/v4`, `mlxrunner`) is for image-generation / MLX-format models. So a 1B text LLM is
GPU-accelerated via Metal here, just not via MLX. Caveat: a 1B is too weak to play CivRealm well
(it plays *legally* under constrained mode but founds no cities); use a larger local model (7–8B)
or `--backend claude` for competent play.

## API contestant (`--backend api`) — the decoder-enforced Claude path

`--backend claude` shells out to the `claude` CLI and needs no API key, but the CLI only
*validates* the per-turn schema and retries. `--backend api` sends the same schema as
`output_config.format` on the Messages API, where it is compiled and constrains generation, so an
out-of-enum option cannot be produced at all:

```bash
export ANTHROPIC_API_KEY=sk-ant-...            # read from the environment; never stored in the tree
.venv/bin/python -m playloop.loop --backend api --model claude-opus-4-8 --max-turns 50
```

Needs the `anthropic` SDK (`uv pip install --python .venv anthropic`); it is imported lazily, so
the other two backends do not require it. This backend reports no cost in dollars —
`summary.json`'s `total_cost_usd` reads 0 on it — but does record real usage:
`input_tokens`/`output_tokens` per turn in `metrics.jsonl` and `total_input_tokens` /
`total_output_tokens` in `summary.json`. Multiply those by the current rate to get the spend.

