#!/usr/bin/env python3
"""Install KDictate into the current user's desktop session.

This installer runs entirely as the invoking user — no pkexec, no sudo,
no root. The only system-level dependency is ``ibus`` (providing the
``ibus`` and ``ibus-daemon`` binaries), and it must be installed through
the distro package manager BEFORE running this script. The preflight
check in ``run_full_install`` surfaces a clear error with install
commands for the common package managers if those binaries are missing.

Everything else lives under the user's ``$HOME``:

* ``~/.local/share/kdictate/`` — runtime source tree + venv + Whisper model
* ``~/.config/systemd/user/`` — user service unit
* ``~/.local/share/dbus-1/services/`` — D-Bus session activation service
* ``~/.local/share/ibus/component/`` — IBus engine metadata
* ``~/.config/environment.d/`` — environment file for IBUS_COMPONENT_PATH
* ``~/.config/plasma-workspace/env/`` — Plasma Wayland environment hook
* ``~/.local/share/applications/`` — toggle .desktop file
* ``~/.config/kglobalshortcutsrc`` — Ctrl+Space binding

Previous versions of this script re-exec'd itself under pkexec and then
juggled privilege drops, PKEXEC_UID lookups, chown-back loops, and symlink-
attack mitigations across every config file write. PR #7 deleted all of
that: the user already owns everything under ``$HOME``, so the installer
just writes files directly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, NoReturn, Sequence

from kdictate import __version__
from kdictate.app_metadata import DEFAULT_MODEL_HF_REPO, DEFAULT_MODEL_NAME
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


@dataclass(frozen=True, slots=True)
class InstallContext:
    """Resolved paths for the current install run.

    All operations are performed as the invoking user, so we no longer
    track install uid/gid or drop-back-to-user shell state. Everything
    is derived from ``$HOME``.
    """

    script_path: Path
    script_dir: Path
    home: Path
    runtime_dir: Path

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
        }


_TOTAL_STEPS = 11
_current_step = 0


def log(message: str) -> None:
    """Emit an installer progress line."""

    print(f"    {message}")


def step(message: str) -> None:
    """Print a numbered progress step (no newline — step_done completes the line)."""

    global _current_step  # noqa: PLW0603
    _current_step += 1
    print(f"  [{_current_step}/{_TOTAL_STEPS}] {message}...", end="", flush=True)


def step_done(detail: str = "") -> None:
    """Complete the current step line with a checkmark."""

    suffix = f" ({detail})" if detail else ""
    print(f" \u2705{suffix}")


def die(message: str) -> NoReturn:
    """Exit with a friendly installer error message."""

    print(f"\n  \u274c  {message}\n", file=sys.stderr)
    raise SystemExit(1)


def require_command(name: str) -> None:
    """Ensure a command is available in PATH."""

    if shutil.which(name) is None:
        die(f"Required command not found: {name}\n\n      Install it and re-run the installer.")


def build_context() -> InstallContext:
    """Resolve the invoking user's home and runtime paths."""

    if os.geteuid() == 0:
        die(
            "The installer must run as your user, not root.\n\n"
            "      KDictate installs everything under your home directory\n"
            "      (~/.local/share, ~/.config) and does not need any root\n"
            "      privileges. Run it without sudo/pkexec:\n\n"
            "        ./install.py"
        )

    script_path = Path(__file__).resolve()
    return InstallContext(
        script_path=script_path,
        script_dir=script_path.parent,
        home=Path.home(),
        runtime_dir=Path.home() / ".local" / "share" / "kdictate",
    )


def run_command(
    command: Sequence[str | Path],
    *,
    env: Mapping[str, str] | None = None,
    capture_output: bool = False,
    quiet: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess in the current (user) context."""

    args = [str(part) for part in command]
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    if quiet and not capture_output:
        capture_output = True
    return subprocess.run(
        args,
        check=check,
        encoding="utf-8",
        errors="replace",
        capture_output=capture_output,
        env=proc_env,
    )


def _ensure_under_home(ctx: InstallContext, destination: Path) -> None:
    """Refuse to install to paths outside the invoking user's HOME.

    Originally a privilege-escalation mitigation when the installer ran
    as root; now a typo guard plus a defensive check against stale
    symlinks at the destination. Resolving *destination* itself (not
    just ``destination.parent``) matters because a prior install or a
    user-planted symlink at the target filename could point outside
    HOME, and writing through that symlink would land the file where
    the symlink points, not where we intended. ``strict=False`` so
    non-existent paths (the common case on a fresh install) are still
    canonicalized based on whatever components DO exist.
    """

    resolved = destination.resolve(strict=False)
    if not resolved.is_relative_to(ctx.home.resolve()):
        die(f"Refusing to write outside home tree: {destination} resolves to {resolved}")


def write_home_file(ctx: InstallContext, destination: Path, text: str, *, mode: int = 0o644) -> None:
    """Write a text file under the user's home tree."""

    _ensure_under_home(ctx, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    destination.chmod(mode)


def copy_home_file(ctx: InstallContext, source: Path, destination: Path, *, mode: int = 0o644) -> None:
    """Copy a file into place under the user's home tree."""

    _ensure_under_home(ctx, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def render_template(source: Path, replacements: Mapping[str, str]) -> str:
    """Render a packaging template via simple token replacement."""

    text = source.read_text(encoding="utf-8")
    for needle, replacement in replacements.items():
        text = text.replace(needle, replacement)
    return text


def install_rendered_file(
    ctx: InstallContext,
    source: Path,
    destination: Path,
    *,
    mode: int = 0o644,
) -> None:
    """Render and install a packaging template."""

    rendered = render_template(source, ctx.replacements)
    write_home_file(ctx, destination, rendered, mode=mode)


def sync_runtime(ctx: InstallContext) -> None:
    """Sync the source tree payload that the runtime venv imports from."""

    ctx.runtime_dir.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "rsync",
            "-a",
            "--delete",
            "--delete-excluded",
            "--exclude=__pycache__",
            f"{ctx.script_dir / 'kdictate'}/",
            f"{ctx.runtime_dir / 'kdictate'}/",
        ],
    )
    copy_home_file(ctx, ctx.script_dir / "requirements.txt", ctx.runtime_dir / "requirements.txt")
    copy_home_file(ctx, ctx.script_dir / "pyproject.toml", ctx.runtime_dir / "pyproject.toml")


def install_python_environment(ctx: InstallContext) -> None:
    """Create the runtime venv and install dependencies plus the editable package."""

    run_command(["python3", "-m", "venv", str(ctx.venv_dir)], quiet=True)
    run_command([ctx.pip_bin, "install", "--upgrade", "pip"], quiet=True)
    run_command([ctx.pip_bin, "install", "-r", ctx.runtime_dir / "requirements.txt"], quiet=True)
    run_command([ctx.pip_bin, "install", "--no-deps", "-e", ctx.runtime_dir], quiet=True)


def download_model(ctx: InstallContext) -> None:
    """Download or verify the Whisper model.

    Always calls snapshot_download, which checks file hashes internally
    and only re-downloads incomplete or missing files. This handles both
    fresh installs and interrupted downloads correctly. Invoked via a
    direct ``subprocess.run`` so ``tqdm``'s TTY progress bar is preserved
    — our ``run_command`` wrapper's ``capture_output`` path would
    otherwise swallow it.
    """

    model_dir = ctx.runtime_dir / DEFAULT_MODEL_NAME
    subprocess.run(
        [
            str(ctx.python_bin),
            "-u",
            "-c",
            (
                "from huggingface_hub import snapshot_download; "
                f"snapshot_download(repo_id={DEFAULT_MODEL_HF_REPO!r}, "
                f"local_dir={str(model_dir)!r})"
            ),
        ],
        check=True,
    )


def next_preload_engines(current_preload: str, engine_id: str) -> str | None:
    """Return the updated preload list or ``None`` when no change is needed."""

    normalized = current_preload.strip()
    engine_token = f"'{engine_id}'"
    if engine_token in normalized:
        return None
    if normalized in {"", "[]", "@as []"}:
        return f"[{engine_token}]"

    clean = normalized.removeprefix("@as ").strip()
    if not clean.endswith("]"):
        raise ValueError(f"Unexpected dconf preload-engines value: {current_preload!r}")
    return f"{clean[:-1]}, {engine_token}]"


def previous_preload_engines(current_preload: str, engine_id: str) -> str | None:
    """Return the preload list with *engine_id* removed, or ``None`` if absent.

    Inverse of :func:`next_preload_engines`. The reset script uses this so
    that uninstalling kdictate does not also wipe other IBus engines the
    user had configured (``ibus-anthy``, ``ibus-pinyin``, etc.). Returns
    the empty-list literal ``"@as []"`` if removing the engine leaves the
    list empty, so the caller can decide between ``dconf write`` and
    ``dconf reset``.

    Note on the ``@as`` GVariant type prefix: dconf may serve back the
    current value as ``"@as ['x', 'y']"`` (the type-annotated form for an
    "array of strings"). We strip the prefix for parsing and intentionally
    do NOT restore it on the non-empty output — ``next_preload_engines``
    has the same omission, and ``dconf write`` accepts both annotated
    and unannotated forms for ``as`` values. The only place we emit the
    annotated form is the empty terminal case ``"@as []"``, where the
    annotation disambiguates from a string-typed empty array.
    """

    normalized = current_preload.strip()
    engine_token = f"'{engine_id}'"
    if not normalized or normalized in {"[]", "@as []"} or engine_token not in normalized:
        return None

    clean = normalized.removeprefix("@as ").strip()
    if not (clean.startswith("[") and clean.endswith("]")):
        raise ValueError(f"Unexpected dconf preload-engines value: {current_preload!r}")

    inner = clean[1:-1]
    parts = [p.strip() for p in inner.split(",")]
    remaining = [p for p in parts if p and p != engine_token]
    if not remaining:
        return "@as []"
    return f"[{', '.join(remaining)}]"


def configure_preload_engines(ctx: InstallContext) -> None:
    """Ensure KDictate appears in the user's IBus preload engine list."""

    result = run_command(
        ["dconf", "read", "/desktop/ibus/general/preload-engines"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        log("dconf read failed; skipping preload-engines update")
        return
    current_preload = result.stdout.strip()
    try:
        new_preload = next_preload_engines(current_preload, DBUS_INTERFACE)
    except ValueError as exc:
        log(f"skipping preload-engines update: {exc}")
        return
    if new_preload is None:
        return  # already present
    run_command(
        ["dconf", "write", "/desktop/ibus/general/preload-engines", new_preload],
    )


def configure_kwin_input_method(ctx: InstallContext) -> None:
    """Point KDE Wayland at IBus Wayland when the KDE helper is available."""

    if shutil.which("kwriteconfig6") is None:
        return

    if KDE_VIRTUAL_KEYBOARD_DESKTOP.is_file():
        run_command(
            [
                "kwriteconfig6",
                "--file",
                ctx.home / ".config" / "kwinrc",
                "--group",
                "Wayland",
                "--key",
                "InputMethod",
                KDE_VIRTUAL_KEYBOARD_DESKTOP,
            ],
        )
    else:
        log(f"Warning: {KDE_VIRTUAL_KEYBOARD_DESKTOP} not found; skipping InputMethod configuration")

    run_command(
        [
            "kwriteconfig6",
            "--file",
            ctx.home / ".config" / "kwinrc",
            "--group",
            "Wayland",
            "--key",
            "VirtualKeyboardEnabled",
            "true",
        ],
    )


def register_global_shortcut(ctx: InstallContext) -> None:
    """Persist a Ctrl+Space entry into kglobalshortcutsrc.

    The running daemon claims Ctrl+Space directly from kwin_wayland via
    its accessibility KeyboardMonitor (see ``kdictate.core.kwin_hotkey``),
    so the live binding does not depend on this file at all. The ini
    entry is written purely as a fallback so KDE's kcontrol shows
    Ctrl+Space as taken by KDictate Toggle, and so a future user that
    disables the KeyboardMonitor grab still has a working shortcut after
    the next session start.
    """

    shortcut_file = ctx.home / ".config" / "kglobalshortcutsrc"
    section = f"[services][{TOGGLE_DESKTOP_NAME}]"
    entry = "_launch=Ctrl+Space, Ctrl+Space"

    content = shortcut_file.read_text(encoding="utf-8") if shortcut_file.exists() else ""
    if section in content:
        return
    content = content.rstrip("\n") + f"\n\n{section}\n{entry}\n"
    write_home_file(ctx, shortcut_file, content)


def refresh_ibus_registry(ctx: InstallContext) -> None:
    """Refresh IBus cache and restart ibus-daemon with the user component path."""

    ibus_env = {
        "IBUS_COMPONENT_PATH": (
            f"{ctx.home / '.local/share/ibus/component'}:/usr/share/ibus/component"
        )
    }
    run_command(["ibus", "write-cache"], env=ibus_env, quiet=True)
    # Toggle KWin's virtual keyboard off/on via D-Bus. This causes KWin to
    # relaunch ibus-ui-gtk3 --enable-wayland-im (which in turn starts
    # ibus-daemon), picking up the updated cache with our component.
    # This avoids the need to sign out and back in.
    if shutil.which("gdbus") is not None:
        vk_dest = "org.kde.KWin"
        vk_path = "/VirtualKeyboard"
        vk_iface = "org.kde.kwin.VirtualKeyboard"
        set_method = "org.freedesktop.DBus.Properties.Set"
        for value in ("false", "true"):
            run_command(
                ["gdbus", "call", "--session",
                 "--dest", vk_dest, "--object-path", vk_path,
                 "--method", set_method, vk_iface, "enabled",
                 f"<boolean {value}>"],
                quiet=True, check=False,
            )


def reload_systemd_user(ctx: InstallContext) -> None:
    """Reload, enable, and restart the KDictate user service."""

    run_command(["systemctl", "--user", "daemon-reload"], quiet=True)
    run_command(["systemctl", "--user", "enable", SERVICE_NAME], quiet=True)
    run_command(["systemctl", "--user", "restart", SERVICE_NAME], quiet=True)


def print_summary(ctx: InstallContext) -> None:
    """Print the install result summary."""

    print(f"\n  \U0001f389 KDictate {__version__} installed successfully!")
    print("     Ctrl+Space to toggle dictation.")
    print()


def run_sync_only(ctx: InstallContext) -> int:
    """Fast dev loop that only syncs runtime sources and restarts the daemon."""

    sync_runtime(ctx)
    result = run_command(
        ["systemctl", "--user", "restart", SERVICE_NAME],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no detail").strip()
        log(f"Service restart skipped or failed (source synced): {detail}")
    log(f"Sync-only complete. RUNTIME_DIR={ctx.runtime_dir}")
    return 0


def preflight_ibus() -> None:
    """Verify the system has ibus installed, with a helpful error if not.

    KDictate used to shell out to ``pacman -S ibus`` under pkexec to
    install ibus as part of the flow. That coupled the whole installer
    to root for what amounts to a one-time system dependency. Now the
    installer runs entirely unprivileged and the user installs ibus
    through their distro's package manager before running this script.
    """

    missing = [
        cmd for cmd in ("ibus", "ibus-daemon") if shutil.which(cmd) is None
    ]
    if not missing:
        return

    die(
        "KDictate needs ibus and ibus-daemon, which are not on PATH.\n\n"
        "      Install them with your distro's package manager and re-run\n"
        "      ./install.py:\n\n"
        "        Arch / Manjaro:  sudo pacman -S --needed ibus\n"
        "        Debian / Ubuntu: sudo apt install ibus\n"
        "        Fedora:          sudo dnf install ibus\n\n"
        "      (Missing: " + ", ".join(missing) + ")"
    )


def run_full_install(ctx: InstallContext) -> int:
    """Perform the full install flow as the invoking user."""

    print(f"\n  KDictate {__version__} installer\n")

    preflight_ibus()
    # gdbus is intentionally NOT required — refresh_ibus_registry already
    # guards its usage with `if shutil.which("gdbus") is not None`, so
    # missing gdbus degrades gracefully (the KWin virtual-keyboard toggle
    # is skipped) instead of blocking the whole install.
    for cmd in ("python3", "systemctl", "rsync", "dconf"):
        require_command(cmd)

    step("Syncing runtime files")
    sync_runtime(ctx)
    step_done()

    step("Setting up Python environment")
    install_python_environment(ctx)
    step_done()

    step("Downloading Whisper model")
    print(flush=True)  # newline so tqdm progress gets its own lines
    download_model(ctx)
    step_done(DEFAULT_MODEL_HF_REPO)

    step("Installing systemd user service")
    install_rendered_file(ctx, ctx.script_dir / "packaging" / "kdictate-systemd.service",
                          ctx.home / ".config/systemd/user" / SERVICE_NAME)
    step_done()

    step("Installing D-Bus activation service")
    install_rendered_file(ctx, ctx.script_dir / "packaging" / f"{APP_ROOT_ID}.service",
                          ctx.home / ".local/share/dbus-1/services" / DBUS_SERVICE_NAME)
    step_done()

    step("Installing IBus engine metadata")
    install_rendered_file(ctx, ctx.script_dir / "packaging" / IBUS_COMPONENT_NAME,
                          ctx.home / ".local/share/ibus/component" / IBUS_COMPONENT_NAME)
    install_rendered_file(ctx, ctx.script_dir / "packaging" / IBUS_ENV_FILE_NAME,
                          ctx.home / ".config/environment.d" / IBUS_ENV_FILE_NAME)
    step_done()

    step("Installing KDE/Plasma integration")
    copy_home_file(ctx, ctx.script_dir / "packaging" / PLASMA_ENV_SCRIPT_NAME,
                   ctx.home / ".config/plasma-workspace/env" / PLASMA_ENV_SCRIPT_NAME)
    install_rendered_file(ctx, ctx.script_dir / "packaging" / TOGGLE_DESKTOP_NAME,
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
    # The KWin toggle relaunches ibus-daemon asynchronously. Retry a few
    # times so the engine activation doesn't race the daemon startup.
    import time as _time
    for attempt in range(5):
        result = run_command(
            ["ibus", "engine", DBUS_INTERFACE],
            quiet=True,
            check=False,
        )
        if result.returncode == 0:
            break
        _time.sleep(1)
    step_done()

    print_summary(ctx)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the installer as the invoking user."""

    args = list(argv if argv is not None else sys.argv[1:])
    ctx = build_context()

    if args == ["--sync-only"]:
        return run_sync_only(ctx)

    return run_full_install(ctx)


if __name__ == "__main__":
    raise SystemExit(main())
