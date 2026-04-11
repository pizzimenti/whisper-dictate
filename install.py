#!/usr/bin/env python3
"""Install KDictate into the current user's desktop session."""

from __future__ import annotations

import os
import pwd
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
    """Resolved paths and ownership for the current install run."""

    script_path: Path
    script_dir: Path
    home: Path
    install_uid: int
    install_gid: int
    runtime_dir: Path

    @property
    def needs_user_shell(self) -> bool:
        return os.geteuid() == 0 and self.install_uid != 0

    @property
    def user_runtime_dir(self) -> Path:
        return Path(f"/run/user/{self.install_uid}")

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


_TOTAL_STEPS = 12
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
    """Resolve the target user, home, and runtime paths."""

    script_path = Path(__file__).resolve()
    script_dir = script_path.parent
    if os.geteuid() == 0 and os.environ.get("PKEXEC_UID"):
        install_uid = int(os.environ["PKEXEC_UID"])
        user_entry = pwd.getpwuid(install_uid)
        home = Path(user_entry.pw_dir)
        install_gid = user_entry.pw_gid
    elif os.geteuid() == 0:
        die("Do not run the installer as root directly; use pkexec")
    else:
        install_uid = os.getuid()
        install_gid = os.getgid()
        home = Path.home()
    return InstallContext(
        script_path=script_path,
        script_dir=script_dir,
        home=home,
        install_uid=install_uid,
        install_gid=install_gid,
        runtime_dir=home / ".local" / "share" / "kdictate",
    )


def run_command(
    ctx: InstallContext,
    command: Sequence[str | Path],
    *,
    as_user: bool = False,
    env: Mapping[str, str] | None = None,
    capture_output: bool = False,
    quiet: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with optional privilege drop back to the user."""

    args = [str(part) for part in command]
    if as_user and ctx.needs_user_shell:
        require_command("sudo")
        user_env = {
            "HOME": str(ctx.home),
            "XDG_RUNTIME_DIR": str(ctx.user_runtime_dir),
        }
        if env:
            user_env.update(env)
        args = [
            "sudo",
            "-u",
            f"#{ctx.install_uid}",
            "env",
            *[f"{key}={value}" for key, value in user_env.items()],
            *args,
        ]
        proc_env = None
    else:
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


def _chown_home_path(ctx: InstallContext, path: Path) -> None:
    """Reassign a path under HOME back to the user after root writes it."""

    if os.geteuid() != 0 or not path.exists():
        return
    current = path.resolve()
    resolved_home = ctx.home.resolve()
    if not current.is_relative_to(resolved_home):
        return
    while current.is_relative_to(resolved_home):
        os.lchown(current, ctx.install_uid, ctx.install_gid)
        if current == resolved_home:
            break
        current = current.parent


def _safe_destination(destination: Path) -> Path:
    """Remove a symlink at *destination* so subsequent writes are not redirected.

    Under pkexec the installer runs as root against a user-controlled home
    directory. A user-planted symlink at the destination would cause root to
    overwrite the symlink target — a local privilege-escalation vector. This
    helper removes the symlink (but not regular files, which are ours from a
    previous install) so the caller can safely create the real file.
    """

    if destination.is_symlink():
        destination.unlink()
    return destination


def write_owned_text(ctx: InstallContext, destination: Path, text: str, *, mode: int = 0o644) -> None:
    """Write a text file under the target user's ownership."""

    resolved_parent = destination.parent.resolve()
    if not resolved_parent.is_relative_to(ctx.home.resolve()):
        die(f"Refusing to write outside home tree: {destination} resolves to {resolved_parent}")
    resolved_parent.mkdir(parents=True, exist_ok=True)
    _chown_home_path(ctx, destination.parent)
    _safe_destination(destination)
    destination.write_text(text, encoding="utf-8")
    destination.chmod(mode)
    _chown_home_path(ctx, destination)


def copy_owned_file(ctx: InstallContext, source: Path, destination: Path, *, mode: int = 0o644) -> None:
    """Copy a file into place under the target user's ownership."""

    resolved_parent = destination.parent.resolve()
    if not resolved_parent.is_relative_to(ctx.home.resolve()):
        die(f"Refusing to write outside home tree: {destination} resolves to {resolved_parent}")
    resolved_parent.mkdir(parents=True, exist_ok=True)
    _chown_home_path(ctx, destination.parent)
    _safe_destination(destination)
    shutil.copyfile(source, destination)
    destination.chmod(mode)
    _chown_home_path(ctx, destination)


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
    write_owned_text(ctx, destination, rendered, mode=mode)


def sync_runtime(ctx: InstallContext) -> None:
    """Sync the source tree payload that the runtime venv imports from."""

    ctx.runtime_dir.mkdir(parents=True, exist_ok=True)
    _chown_home_path(ctx, ctx.runtime_dir)
    run_command(
        ctx,
        [
            "rsync",
            "-a",
            "--delete",
            "--delete-excluded",
            "--exclude=__pycache__",
            f"{ctx.script_dir / 'kdictate'}/",
            f"{ctx.runtime_dir / 'kdictate'}/",
        ],
        as_user=True,
    )
    copy_owned_file(ctx, ctx.script_dir / "requirements.txt", ctx.runtime_dir / "requirements.txt")
    copy_owned_file(ctx, ctx.script_dir / "pyproject.toml", ctx.runtime_dir / "pyproject.toml")


def install_python_environment(ctx: InstallContext) -> None:
    """Create the runtime venv and install dependencies plus the editable package."""

    run_command(ctx, ["python3", "-m", "venv", str(ctx.venv_dir)], as_user=True, quiet=True)
    run_command(ctx, [ctx.pip_bin, "install", "--upgrade", "pip"], as_user=True, quiet=True)
    run_command(ctx, [ctx.pip_bin, "install", "-r", ctx.runtime_dir / "requirements.txt"], as_user=True, quiet=True)
    run_command(ctx, [ctx.pip_bin, "install", "--no-deps", "-e", ctx.runtime_dir], as_user=True, quiet=True)


def download_model(ctx: InstallContext) -> None:
    """Download or verify the Whisper model.

    Always calls snapshot_download, which checks file hashes internally
    and only re-downloads incomplete or missing files. This handles both
    fresh installs and interrupted downloads correctly.
    """

    model_dir = ctx.runtime_dir / DEFAULT_MODEL_NAME

    # Run the download outside run_command so the TTY is preserved for
    # tqdm progress bars. subprocess user=/group= drops privileges
    # cleanly without the sudo wrapper that kills the PTY.
    dl_env = os.environ.copy()
    dl_env["HOME"] = str(ctx.home)
    dl_env["XDG_RUNTIME_DIR"] = str(ctx.user_runtime_dir)
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
        env=dl_env,
        user=ctx.install_uid if os.geteuid() == 0 else None,
        group=ctx.install_gid if os.geteuid() == 0 else None,
    )
    _chown_home_path(ctx, model_dir)


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


def configure_preload_engines(ctx: InstallContext) -> None:
    """Ensure KDictate appears in the user's IBus preload engine list."""

    result = run_command(
        ctx,
        ["dconf", "read", "/desktop/ibus/general/preload-engines"],
        as_user=True,
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
        pass  # already present
        return
    run_command(
        ctx,
        ["dconf", "write", "/desktop/ibus/general/preload-engines", new_preload],
        as_user=True,
    )


def configure_kwin_input_method(ctx: InstallContext) -> None:
    """Point KDE Wayland at IBus Wayland when the KDE helper is available."""

    if shutil.which("kwriteconfig6") is None:
        return

    # configure_kwin_input_method runs silently under the parent step
    if KDE_VIRTUAL_KEYBOARD_DESKTOP.is_file():
        run_command(
            ctx,
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
            as_user=True,
        )
    else:
        log(f"Warning: {KDE_VIRTUAL_KEYBOARD_DESKTOP} not found; skipping InputMethod configuration")

    run_command(
        ctx,
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
        as_user=True,
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
    shortcut_file.write_text(content, encoding="utf-8")


def refresh_ibus_registry(ctx: InstallContext) -> None:
    """Refresh IBus cache and restart ibus-daemon with the user component path."""

    ibus_env = {
        "IBUS_COMPONENT_PATH": (
            f"{ctx.home / '.local/share/ibus/component'}:/usr/share/ibus/component"
        )
    }
    run_command(ctx, ["ibus", "write-cache"], as_user=True, env=ibus_env, quiet=True)
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
                ctx,
                ["gdbus", "call", "--session",
                 "--dest", vk_dest, "--object-path", vk_path,
                 "--method", set_method, vk_iface, "enabled",
                 f"<boolean {value}>"],
                as_user=True, quiet=True, check=False,
            )


def reload_systemd_user(ctx: InstallContext) -> None:
    """Reload, enable, and restart the KDictate user service."""

    run_command(ctx, ["systemctl", "--user", "daemon-reload"], as_user=True, quiet=True)
    run_command(ctx, ["systemctl", "--user", "enable", SERVICE_NAME], as_user=True, quiet=True)
    run_command(ctx, ["systemctl", "--user", "restart", SERVICE_NAME], as_user=True, quiet=True)


def print_summary(ctx: InstallContext) -> None:
    """Print the install result summary."""

    print(f"\n  \U0001f389 KDictate {__version__} installed successfully!")
    print("     Ctrl+Space to toggle dictation.")
    print()


def run_sync_only(ctx: InstallContext) -> int:
    """Fast dev loop that only syncs runtime sources and restarts the daemon."""

    if os.geteuid() == 0:
        die("--sync-only must run as your user, not root")
    sync_runtime(ctx)
    result = run_command(
        ctx,
        ["systemctl", "--user", "restart", SERVICE_NAME],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no detail").strip()
        log(f"Service restart skipped or failed (source synced): {detail}")
    log(f"Sync-only complete. RUNTIME_DIR={ctx.runtime_dir}")
    return 0


def run_full_install(ctx: InstallContext) -> int:
    """Perform the full root-assisted install flow."""

    print(f"\n  KDictate {__version__} installer\n")

    for cmd in ("pacman", "python3", "systemctl", "gdbus", "rsync"):
        require_command(cmd)

    step("Installing system dependencies")
    run_command(ctx, ["pacman", "-S", "--noconfirm", "--needed", "ibus"], quiet=True)
    require_command("dconf")
    require_command("ibus")
    require_command("ibus-daemon")
    step_done("ibus")

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
    copy_owned_file(ctx, ctx.script_dir / "packaging" / PLASMA_ENV_SCRIPT_NAME,
                    ctx.home / ".config/plasma-workspace/env" / PLASMA_ENV_SCRIPT_NAME)
    install_rendered_file(ctx, ctx.script_dir / "packaging" / TOGGLE_DESKTOP_NAME,
                          ctx.home / ".local/share/applications" / TOGGLE_DESKTOP_NAME)
    if shutil.which("kbuildsycoca6") is not None:
        run_command(ctx, ["kbuildsycoca6", "--noincremental"], as_user=True, quiet=True, check=False)
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
            ctx,
            ["ibus", "engine", DBUS_INTERFACE],
            as_user=True,
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
    """Run the installer."""

    args = list(argv if argv is not None else sys.argv[1:])
    ctx = build_context()

    if args == ["--sync-only"]:
        return run_sync_only(ctx)

    if os.geteuid() != 0:
        os.execvp("pkexec", ["pkexec", sys.executable, str(ctx.script_path), *args])

    return run_full_install(ctx)


if __name__ == "__main__":
    raise SystemExit(main())
