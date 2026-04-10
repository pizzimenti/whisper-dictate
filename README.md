# KDictate

Local Whisper dictation for KDE Plasma Wayland via IBus.

KDictate is architecturally distinct from generic `whisper-copy-paste`-style tools: it
solves text placement correctly on **KDE Plasma Wayland** by going through IBus and
the KWin input-method protocol, rather than relying on synthetic-keystroke injectors
or clipboard hacks. The `K`-prefix advertises the target environment.

The system is split into two cooperating processes that talk over session D-Bus:

1. **Core dictation daemon** (`kdictate-daemon` / `python -m kdictate.core`): persistent mic
   capture/transcribe worker that publishes transcript/state events on the
   session D-Bus.
2. **IBus frontend** (`ibus-engine-kdictate` / `python -m kdictate.ibus_engine`): the only component
   allowed to place text into applications; it consumes daemon transcript
   events and maps them to IBus preedit and commit.

This project uses `openai/whisper-large-v3-turbo` converted to CTranslate2 int8 for
local English dictation on CPU.

> KWin Wayland + IBus is the supported configuration. On GNOME or other Wayland
> compositors the daemon + IBus engine path *may* work but is untested.

## Quick start

```bash
cd kdictate
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
# optional, for local test/dev tooling such as pytest
pip install -r requirements-dev.txt
```

### System dictation daemon

On Arch/Manjaro, `install.py` handles the bootstrap path automatically:

```bash
python3 install.py
```

It installs `ibus`, sets up the Python environment, registers the
`io.github.pizzimenti.KDictate.service` systemd user unit, installs the D-Bus
activation file, places the IBus component metadata under the current user's data
directory, writes `~/.config/environment.d/60-kdictate-ibus.conf` so IBus can
scan that per-user component directory, keeps `XMODIFIERS=@im=ibus` for
XWayland/X11 compatibility, installs
`~/.config/plasma-workspace/env/kdictate-plasma-wayland.sh` to unset
`GTK_IM_MODULE` and `QT_IM_MODULE` during Plasma Wayland startup, installs the
project into `~/.local/share/kdictate/.venv` as an editable package so the
IBus component can execute `~/.local/share/kdictate/.venv/bin/ibus-engine-kdictate`,
installs a hidden KDE launcher that calls `gdbus` directly to toggle dictation, configures
Plasma's KWin Wayland input method in `~/.config/kwinrc` to use the installed
`IBus Wayland` desktop file, refreshes the IBus cache, and restarts
`ibus-daemon` for the current session.

To start the daemon manually instead:

```bash
source .venv/bin/activate
python -m kdictate.core
```

The daemon publishes the reverse-DNS session bus name
`io.github.pizzimenti.KDictate1`, and the IBus frontend is expected to be
selected by that engine name once the frontend package is installed.

The core service is user-level and idempotent:

```bash
systemctl --user enable --now io.github.pizzimenti.KDictate.service
```

If you want to confirm the D-Bus API manually, use the terminal control path
once the daemon-side service is available:

```bash
python -m kdictate.cli start
# speak
python -m kdictate.cli stop
```

`stop` waits for transcription to finish and prints the latest transcript from
the daemon runtime files.

### IBus selection flow

After the IBus frontend is installed, enable it the same way you would any other IBus engine:

1. Open your IBus configuration tool.
2. Add `KDictate` or the reverse-DNS engine name `io.github.pizzimenti.KDictate1`.
3. Select it in the input method switcher when you want dictation text to flow
   into the focused application.

The daemon never inserts text directly. Partial transcript should appear as
preedit and final transcript should be committed by the IBus frontend only.
If the engine still does not appear immediately after install, or if text
fields do not accept dictation commits, sign out and back in once so the
desktop session reloads the updated input-method environment and KWin picks up
the configured IBus Wayland input method. On Plasma Wayland, `GTK_IM_MODULE`
and `QT_IM_MODULE` should remain unset in the desktop session; the
compositor-backed `IBus Wayland` path handles native Wayland clients.

On KDE Plasma, `io.github.pizzimenti.KDictateToggle.desktop` can be bound as a
global shortcut. Its `Exec=` line calls `gdbus` directly against the session
D-Bus, bypassing Python startup for the toggle hot path — this makes
`Ctrl+Space` noticeably snappier than a Python-wrapped toggle.

## Architecture

- `kdictate/core/daemon.py` (`python -m kdictate.core`): long-lived daemon that
  keeps the Whisper model warm, owns microphone capture, VAD segmentation,
  transcription, and publishes state/transcript events over session D-Bus.
- `kdictate/ibus_engine/` (`python -m kdictate.ibus_engine`): IBus engine
  process that subscribes to daemon D-Bus signals and maps them to IBus
  preedit/commit. This is the **only** path allowed to place text into
  applications.
- `kdictate/cli/dictatectl.py` (`python -m kdictate.cli`): stdlib D-Bus control
  plane for `start`, `stop`, `toggle`, `status`, and `last-text`. Use this from
  a terminal for scripting. For the `Ctrl+Space` hot path, the installed
  `.desktop` file calls `gdbus` directly instead.
- `kdictate/`: package providing constants, exceptions, logging, D-Bus API
  definition, and the service/IBus/CLI subpackages.

### Runtime files

The daemon and helpers coordinate through two files under `XDG_RUNTIME_DIR`:

- `kdictate-<uid>.state`: current daemon state (`idle`, `starting`, `recording`, `transcribing`, or `error`)
- `kdictate-<uid>.last.txt`: latest completed transcript

The daemon owns writes to those files. The CLI reads them so control/status
behavior stays consistent across shells and user services.

## Tuning

- `--profile interactive|service`: named daemon presets. The installed user service uses `service`; ad-hoc CLI runs default to `interactive`.
- `--cpu-threads N`: override thread count. Dictation-oriented defaults now use physical cores / short-form-friendly thread counts.
- `--compute-type int8|float16|float32`: precision/runtime tradeoff.
- `--language`: defaults to `en`.
- `--beam-size`: daemon and live CLI default to 1.
- `--state-file`: daemon runtime state file path (default: `$XDG_RUNTIME_DIR/kdictate-<uid>.state`).
- `--last-text-file`: latest transcript cache path (default: `$XDG_RUNTIME_DIR/kdictate-<uid>.last.txt`).
- `--vad-filter/--no-vad-filter`: daemon defaults to `vad_filter=False` for lower-latency short-form dictation.
- `--condition-on-previous-text/--no-condition-on-previous-text`: daemon defaults to `False` to reduce cascading hallucinations.
- `--no-speech-threshold`: Whisper-side non-speech rejection. The daemon defaults to `0.6`.
- `--energy-threshold`, `--start-speech-ms`, `--silence-ms`, `--max-utterance-s`: live CLI utterance-boundary controls.
- `--task transcribe|translate`: keep original language vs force English output (CLI mode only).
- `--decode-workers`, `--diag`, `--diag-interval-s`: parallelism and diagnostics (CLI mode only).
- Runtime control/VAD polling now uses 150ms wait intervals to reduce idle wakeups without materially affecting dictation latency.

## Files

- `install.py`: install dependencies, register the user service, and install the D-Bus and IBus metadata (Arch/Manjaro).
- `pyproject.toml`: package metadata and console-script entry points.
- `requirements-dev.txt`: local development and test-only dependencies.
- `kdictate/`: core package — D-Bus contract, daemon logic, IBus frontend, CLI, runtime utilities, audio helpers (`kdictate.audio_common`), and CPU thread / compute-type selection (`kdictate.runtime_profile`).
- `packaging/kdictate-systemd.service`: systemd user unit for the core daemon (`ExecStart=... kdictate-daemon --profile service`).
- `packaging/io.github.pizzimenti.KDictate.service`: D-Bus session activation file (delegates to the systemd unit via `SystemdService=`).
- `packaging/io.github.pizzimenti.KDictate.xml`: D-Bus introspection XML published on the session bus.
- `packaging/io.github.pizzimenti.KDictate.component.xml`: IBus component metadata for the engine frontend.
- `packaging/io.github.pizzimenti.KDictateToggle.desktop`: hidden KDE application entry that binds `Ctrl+Space` to a direct `gdbus call` against the session bus.
- `packaging/60-kdictate-ibus.conf`: `environment.d` snippet that adds the per-user IBus component directory to `IBUS_COMPONENT_PATH` and sets `XMODIFIERS=@im=ibus`.
- `packaging/kdictate-plasma-wayland.sh`: Plasma session env script that unsets `GTK_IM_MODULE` and `QT_IM_MODULE` to let the compositor-backed IBus Wayland path handle native clients.
- `check_ibus_only.py`: regression check for forbidden injector and clipboard backends.

## Notes

- CPU-only; no CUDA or ROCm required.
- The model (`whisper-large-v3-turbo`, CTranslate2 int8) is stored at `~/.local/share/kdictate/whisper-large-v3-turbo-ct2/`.
