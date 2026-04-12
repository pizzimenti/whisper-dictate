#!/usr/bin/env python3
"""Install KDictate into the current user's desktop session.

Runs entirely as the invoking user — no root required.  The only
system-level dependency is ``ibus``, which must be installed via the
distro package manager before running this script.

Everything lives under ``$HOME``:

* ``~/.local/share/kdictate/`` — runtime source tree + venv + models
* ``~/.config/systemd/user/`` — user service unit
* ``~/.local/share/dbus-1/services/`` — D-Bus session activation
* ``~/.local/share/ibus/component/`` — IBus engine metadata
* ``~/.config/environment.d/`` — IBUS_COMPONENT_PATH
* ``~/.config/plasma-workspace/env/`` — Plasma Wayland env hook
* ``~/.local/share/applications/`` — toggle .desktop file
* ``~/.config/kglobalshortcutsrc`` — Ctrl+Space binding
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, NoReturn

from kdictate import __version__
from kdictate.app_metadata import (
    DEFAULT_MODEL_HF_REPO,
    DEFAULT_MODEL_NAME,
    GGML_MODEL_FILENAME,
    GGML_MODEL_HF_REPO,
    GGML_MODEL_PATH,
)
from kdictate.constants import APP_ROOT_ID, DBUS_INTERFACE

SERVICE_NAME = f"{APP_ROOT_ID}.service"
DBUS_SERVICE_NAME = f"{DBUS_INTERFACE}.service"
IBUS_COMPONENT_NAME = f"{APP_ROOT_ID}.component.xml"
TOGGLE_DESKTOP_NAME = f"{APP_ROOT_ID}Toggle.desktop"
IBUS_ENV_FILE_NAME = "60-kdictate-ibus.conf"
PLASMA_ENV_SCRIPT_NAME = "kdictate-plasma-wayland.sh"
KDE_VIRTUAL_KEYBOARD_DESKTOP = Path(
    "/usr/share/applications/org.freedesktop.IBus.Panel.Wayland.Gtk3.desktop"
)


# -------------------------------------------------------------------
# Context
# -------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class InstallContext:
    script_path: Path
    script_dir: Path
    home: Path
    runtime_dir: Path
    gpu: bool = False

    @property
    def venv_dir(self) -> Path:
        return self.runtime_dir / ".venv"

    @property
    def python_bin(self) -> Path:
        return self.venv_dir / "bin" / "python"

    @property
    def pip_bin(self) -> Path:
        return self.venv_dir / "bin" / "pip"

    @property
    def engine_exec(self) -> Path:
        return self.venv_dir / "bin" / "ibus-engine-kdictate"

    @property
    def replacements(self) -> Mapping[str, str]:
        return {
            "@@REPO_DIR@@": str(self.runtime_dir),
            "@@ENGINE_EXEC@@": str(self.engine_exec),
            "@@HOME@@": str(self.home),
            "@@APP_VERSION@@": __version__,
            "@@BACKEND_FLAGS@@": " --backend auto" if self.gpu else "",
        }


# -------------------------------------------------------------------
# UI helpers
# -------------------------------------------------------------------

_TOTAL_STEPS = 11
_current_step = 0


def log(message: str) -> None:
    print(f"    {message}")


def step(message: str) -> None:
    global _current_step  # noqa: PLW0603
    _current_step += 1
    print(f"  \u2705 [{_current_step}/{_TOTAL_STEPS}] {message}", end="", flush=True)


def step_done(detail: str = "") -> None:
    print(f" ({detail})" if detail else "")


def die(message: str) -> NoReturn:
    print(f"\n  \u274c  {message}\n", file=sys.stderr)
    raise SystemExit(1)


# -------------------------------------------------------------------
# Distro detection
# -------------------------------------------------------------------

def _detect_distro() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8").lower()
    except FileNotFoundError:
        return "unknown"
    if "arch" in text or "manjaro" in text or "endeavour" in text:
        return "arch"
    if "ubuntu" in text or "debian" in text or "mint" in text:
        return "debian"
    if "fedora" in text or "rhel" in text or "centos" in text:
        return "fedora"
    return "unknown"


def _pkg_hint(distro: str, pkg: str) -> str:
    """Return the distro-appropriate install command for *pkg*."""
    if distro == "arch":
        return f"sudo pacman -S --needed {pkg}"
    if distro == "debian":
        return f"sudo apt install {pkg}"
    if distro == "fedora":
        return f"sudo dnf install {pkg}"
    return f"install {pkg} with your package manager"


# -------------------------------------------------------------------
# GPU detection and prompt
# -------------------------------------------------------------------

def _detect_gpu() -> tuple[str | None, list[str]]:
    """Return (whisper_cpp_binary, reasons_unavailable)."""
    reasons: list[str] = []
    distro = _detect_distro()

    binary = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
    if binary is None:
        if distro == "arch":
            hint = "yay -S whisper.cpp-vulkan"
        else:
            hint = "build whisper.cpp from source with -DGGML_VULKAN=1"
        reasons.append(f"whisper.cpp not found on PATH\n        Install:  {hint}")

    if shutil.which("vulkaninfo") is None:
        reasons.append(
            f"vulkaninfo not found (needed to verify GPU)\n"
            f"        Install:  {_pkg_hint(distro, 'vulkan-tools')}"
        )
    elif binary is not None:
        try:
            r = subprocess.run(["vulkaninfo", "--summary"],
                               capture_output=True, timeout=5)
            if r.returncode != 0:
                reasons.append("vulkaninfo failed — no Vulkan-capable GPU detected")
        except (OSError, subprocess.TimeoutExpired):
            reasons.append("vulkaninfo timed out or crashed")

    return binary, reasons


def _prompt_backend() -> bool:
    """Auto-detect GPU and ask the user.  Returns True for GPU mode."""
    binary, reasons = _detect_gpu()

    if not reasons:
        print("  GPU acceleration is available:\n")
        print(f"    whisper.cpp: {binary}")
        print("    Vulkan:      supported\n")
        print("    [1] GPU mode  (whisper.cpp + Vulkan, faster)")
        print("    [2] CPU mode  (faster-whisper, no extra deps)\n")
        while True:
            choice = input("  Select [1/2]: ").strip()
            if choice == "1":
                return True
            if choice == "2":
                return False
    else:
        print("  GPU acceleration is not available:\n")
        for reason in reasons:
            print(f"    - {reason}")
        print()
        while True:
            choice = input("  Proceed with CPU-only install? [Y/n]: ").strip().lower()
            if choice in ("", "y", "yes"):
                return False
            if choice in ("n", "no"):
                die("Install cancelled.")

    return False


# -------------------------------------------------------------------
# Shell helpers
# -------------------------------------------------------------------

def require_command(name: str) -> None:
    if shutil.which(name) is None:
        distro = _detect_distro()
        die(f"Required command not found: {name}\n\n      {_pkg_hint(distro, name)}")


def run_command(
    command: list[str | Path], *,
    env: Mapping[str, str] | None = None,
    quiet: bool = False, check: bool = True,
) -> subprocess.CompletedProcess[str]:
    args = [str(p) for p in command]
    proc_env = {**os.environ, **(env or {})}
    return subprocess.run(
        args, check=check, encoding="utf-8", errors="replace",
        capture_output=quiet, env=proc_env,
    )


# -------------------------------------------------------------------
# File helpers
# -------------------------------------------------------------------

def _ensure_under_home(ctx: InstallContext, dest: Path) -> None:
    resolved = dest.resolve(strict=False)
    if not resolved.is_relative_to(ctx.home.resolve()):
        die(f"Refusing to write outside home: {dest} -> {resolved}")


def write_home_file(ctx: InstallContext, dest: Path, text: str, *, mode: int = 0o644) -> None:
    _ensure_under_home(ctx, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    dest.chmod(mode)


def copy_home_file(ctx: InstallContext, src: Path, dest: Path, *, mode: int = 0o644) -> None:
    _ensure_under_home(ctx, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    dest.chmod(mode)


def render_template(src: Path, replacements: Mapping[str, str]) -> str:
    text = src.read_text(encoding="utf-8")
    for needle, replacement in replacements.items():
        text = text.replace(needle, replacement)
    return text


def install_rendered_file(ctx: InstallContext, src: Path, dest: Path, *, mode: int = 0o644) -> None:
    write_home_file(ctx, dest, render_template(src, ctx.replacements), mode=mode)


# -------------------------------------------------------------------
# Install steps
# -------------------------------------------------------------------

def sync_runtime(ctx: InstallContext) -> None:
    ctx.runtime_dir.mkdir(parents=True, exist_ok=True)
    run_command([
        "rsync", "-a", "--delete", "--delete-excluded", "--exclude=__pycache__",
        f"{ctx.script_dir / 'kdictate'}/", f"{ctx.runtime_dir / 'kdictate'}/",
    ])
    copy_home_file(ctx, ctx.script_dir / "requirements.txt", ctx.runtime_dir / "requirements.txt")
    copy_home_file(ctx, ctx.script_dir / "pyproject.toml", ctx.runtime_dir / "pyproject.toml")


def install_python_environment(ctx: InstallContext) -> None:
    run_command(["python3", "-m", "venv", str(ctx.venv_dir)], quiet=True)
    run_command([ctx.pip_bin, "install", "--upgrade", "pip"], quiet=True)
    run_command([ctx.pip_bin, "install", "-r", ctx.runtime_dir / "requirements.txt"], quiet=True)
    run_command([ctx.pip_bin, "install", "--no-deps", "-e", ctx.runtime_dir], quiet=True)


def download_cpu_model(ctx: InstallContext) -> None:
    model_dir = ctx.runtime_dir / DEFAULT_MODEL_NAME
    subprocess.run([
        str(ctx.python_bin), "-u", "-c",
        f"from huggingface_hub import snapshot_download; "
        f"snapshot_download(repo_id={DEFAULT_MODEL_HF_REPO!r}, "
        f"local_dir={str(model_dir)!r})",
    ], check=True)


def download_gpu_model(ctx: InstallContext) -> None:
    if GGML_MODEL_PATH.is_file():
        log(f"GGML model already present")
        return
    GGML_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        str(ctx.python_bin), "-u", "-c",
        f"from huggingface_hub import hf_hub_download; "
        f"hf_hub_download(repo_id={GGML_MODEL_HF_REPO!r}, "
        f"filename={GGML_MODEL_FILENAME!r}, "
        f"local_dir={str(GGML_MODEL_PATH.parent)!r})",
    ], check=True)


def next_preload_engines(current: str, engine_id: str) -> str | None:
    normalized = current.strip()
    token = f"'{engine_id}'"
    if token in normalized:
        return None
    if normalized in {"", "[]", "@as []"}:
        return f"[{token}]"
    clean = normalized.removeprefix("@as ").strip()
    if not clean.endswith("]"):
        raise ValueError(f"Unexpected preload-engines value: {current!r}")
    return f"{clean[:-1]}, {token}]"


def previous_preload_engines(current: str, engine_id: str) -> str | None:
    """Return the preload list with *engine_id* removed, or None if absent.

    Used by ``reset-kdictate-install.sh`` to cleanly remove KDictate
    without wiping other IBus engines the user had configured.
    """
    normalized = current.strip()
    token = f"'{engine_id}'"
    if not normalized or normalized in {"[]", "@as []"} or token not in normalized:
        return None
    clean = normalized.removeprefix("@as ").strip()
    if not (clean.startswith("[") and clean.endswith("]")):
        raise ValueError(f"Unexpected preload-engines value: {current!r}")
    parts = [p.strip() for p in clean[1:-1].split(",")]
    remaining = [p for p in parts if p and p != token]
    return f"[{', '.join(remaining)}]" if remaining else "@as []"


def configure_preload_engines(ctx: InstallContext) -> None:
    result = run_command(
        ["dconf", "read", "/desktop/ibus/general/preload-engines"],
        quiet=True, check=False,
    )
    if result.returncode != 0:
        return
    try:
        new = next_preload_engines(result.stdout.strip(), DBUS_INTERFACE)
    except ValueError as exc:
        log(f"skipping preload-engines: {exc}")
        return
    if new is not None:
        run_command(["dconf", "write", "/desktop/ibus/general/preload-engines", new])


def configure_kwin_input_method(ctx: InstallContext) -> None:
    if shutil.which("kwriteconfig6") is None:
        return
    if KDE_VIRTUAL_KEYBOARD_DESKTOP.is_file():
        run_command([
            "kwriteconfig6", "--file", ctx.home / ".config" / "kwinrc",
            "--group", "Wayland", "--key", "InputMethod",
            KDE_VIRTUAL_KEYBOARD_DESKTOP,
        ])
    else:
        log(f"Warning: {KDE_VIRTUAL_KEYBOARD_DESKTOP} not found")
    run_command([
        "kwriteconfig6", "--file", ctx.home / ".config" / "kwinrc",
        "--group", "Wayland", "--key", "VirtualKeyboardEnabled", "true",
    ])


def register_global_shortcut(ctx: InstallContext) -> None:
    shortcut_file = ctx.home / ".config" / "kglobalshortcutsrc"
    section = f"[services][{TOGGLE_DESKTOP_NAME}]"
    entry = "_launch=Ctrl+Space, Ctrl+Space"
    content = shortcut_file.read_text(encoding="utf-8") if shortcut_file.exists() else ""
    if section in content:
        return
    content = content.rstrip("\n") + f"\n\n{section}\n{entry}\n"
    write_home_file(ctx, shortcut_file, content)


def refresh_ibus_registry(ctx: InstallContext) -> None:
    ibus_env = {
        "IBUS_COMPONENT_PATH": (
            f"{ctx.home / '.local/share/ibus/component'}:/usr/share/ibus/component"
        )
    }
    run_command(["ibus", "write-cache"], env=ibus_env, quiet=True)
    if shutil.which("gdbus") is not None:
        for value in ("false", "true"):
            run_command([
                "gdbus", "call", "--session",
                "--dest", "org.kde.KWin", "--object-path", "/VirtualKeyboard",
                "--method", "org.freedesktop.DBus.Properties.Set",
                "org.kde.kwin.VirtualKeyboard", "enabled",
                f"<boolean {value}>",
            ], quiet=True, check=False)


def reload_systemd_user(ctx: InstallContext) -> None:
    run_command(["systemctl", "--user", "daemon-reload"], quiet=True)
    run_command(["systemctl", "--user", "enable", SERVICE_NAME], quiet=True)
    run_command(["systemctl", "--user", "restart", SERVICE_NAME], quiet=True)


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

def main() -> int:
    if os.geteuid() == 0:
        die(
            "Run as your user, not root.\n\n"
            "      KDictate installs under ~/. No root needed:\n\n"
            "        python3 install.py"
        )

    script_path = Path(__file__).resolve()
    ctx = InstallContext(
        script_path=script_path,
        script_dir=script_path.parent,
        home=Path.home(),
        runtime_dir=Path.home() / ".local" / "share" / "kdictate",
    )

    print(f"\n  KDictate {__version__} installer\n")

    gpu = _prompt_backend()
    if gpu:
        ctx = InstallContext(
            script_path=ctx.script_path, script_dir=ctx.script_dir,
            home=ctx.home, runtime_dir=ctx.runtime_dir, gpu=True,
        )

    global _TOTAL_STEPS  # noqa: PLW0603
    if gpu:
        _TOTAL_STEPS += 1

    print()
    preflight_ibus()
    for cmd in ("python3", "systemctl", "rsync", "dconf"):
        require_command(cmd)

    pkg = ctx.script_dir / "packaging"

    step("Syncing runtime files")
    sync_runtime(ctx)
    step_done()

    step("Setting up Python environment")
    install_python_environment(ctx)
    step_done()

    step("Downloading CPU model")
    print(flush=True)
    download_cpu_model(ctx)
    step_done(DEFAULT_MODEL_HF_REPO)

    if gpu:
        step("Downloading GPU model")
        print(flush=True)
        download_gpu_model(ctx)
        step_done(GGML_MODEL_HF_REPO)

    step("Installing systemd user service")
    install_rendered_file(ctx, pkg / "kdictate-systemd.service",
                          ctx.home / ".config/systemd/user" / SERVICE_NAME)
    step_done()

    step("Installing D-Bus activation service")
    install_rendered_file(ctx, pkg / f"{APP_ROOT_ID}.service",
                          ctx.home / ".local/share/dbus-1/services" / DBUS_SERVICE_NAME)
    step_done()

    step("Installing IBus engine metadata")
    install_rendered_file(ctx, pkg / IBUS_COMPONENT_NAME,
                          ctx.home / ".local/share/ibus/component" / IBUS_COMPONENT_NAME)
    install_rendered_file(ctx, pkg / IBUS_ENV_FILE_NAME,
                          ctx.home / ".config/environment.d" / IBUS_ENV_FILE_NAME)
    step_done()

    step("Installing KDE/Plasma integration")
    copy_home_file(ctx, pkg / PLASMA_ENV_SCRIPT_NAME,
                   ctx.home / ".config/plasma-workspace/env" / PLASMA_ENV_SCRIPT_NAME)
    install_rendered_file(ctx, pkg / TOGGLE_DESKTOP_NAME,
                          ctx.home / ".local/share/applications" / TOGGLE_DESKTOP_NAME)
    if shutil.which("kbuildsycoca6") is not None:
        run_command(["kbuildsycoca6", "--noincremental"], quiet=True, check=False)
    register_global_shortcut(ctx)
    step_done()

    step("Registering IBus input method")
    configure_preload_engines(ctx)
    configure_kwin_input_method(ctx)
    step_done()

    step("Refreshing IBus engine registry")
    refresh_ibus_registry(ctx)
    step_done()

    step("Starting KDictate service")
    reload_systemd_user(ctx)
    step_done()

    step("Activating KDictate input method")
    for _ in range(5):
        if run_command(["ibus", "engine", DBUS_INTERFACE], quiet=True, check=False).returncode == 0:
            break
        time.sleep(1)
    step_done()

    mode = "GPU + CPU fallback" if gpu else "CPU only"
    print(f"\n  \U0001f389 KDictate {__version__} installed ({mode})")
    print("     Ctrl+Space to toggle dictation.\n")
    return 0


def preflight_ibus() -> None:
    missing = [cmd for cmd in ("ibus", "ibus-daemon") if shutil.which(cmd) is None]
    if not missing:
        return
    distro = _detect_distro()
    die(
        "KDictate needs ibus and ibus-daemon.\n\n"
        f"      Install:  {_pkg_hint(distro, 'ibus')}\n\n"
        "      (Missing: " + ", ".join(missing) + ")"
    )


if __name__ == "__main__":
    raise SystemExit(main())
