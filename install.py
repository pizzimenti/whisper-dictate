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


def log(message: str) -> None:
    """Emit an installer progress line."""

    print(f"==> {message}")


def die(message: str) -> NoReturn:
    """Exit with a consistent installer error message."""

    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def require_command(name: str) -> None:
    """Ensure a command is available in PATH."""

    if shutil.which(name) is None:
        die(f"Missing required command: {name}")


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
    log(f"Syncing source files to {ctx.runtime_dir}")
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

    log(f"Creating Python virtual environment in {ctx.venv_dir}")
    run_command(ctx, ["python3", "-m", "venv", str(ctx.venv_dir)], as_user=True)

    log("Installing Python dependencies")
    run_command(ctx, [ctx.pip_bin, "install", "--upgrade", "pip"], as_user=True)
    run_command(ctx, [ctx.pip_bin, "install", "-r", ctx.runtime_dir / "requirements.txt"], as_user=True)
    run_command(ctx, [ctx.pip_bin, "install", "--no-deps", "-e", ctx.runtime_dir], as_user=True)


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

    log("Registering KDictate in IBus preload-engines (if missing)")
    result = run_command(
        ctx,
        ["dconf", "read", "/desktop/ibus/general/preload-engines"],
        as_user=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        log("  dconf read failed; skipping preload-engines update")
        return
    current_preload = result.stdout.strip()
    try:
        new_preload = next_preload_engines(current_preload, DBUS_INTERFACE)
    except ValueError as exc:
        log(f"  skipping preload-engines update: {exc}")
        return
    if new_preload is None:
        log("  already present; leaving preload-engines unchanged")
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

    log("Configuring KDE Wayland to use IBus Wayland as the virtual keyboard")
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


def refresh_ibus_registry(ctx: InstallContext) -> None:
    """Refresh IBus cache and restart ibus-daemon with the user component path."""

    log("Refreshing the IBus engine registry for the current session")
    ibus_env = {
        "IBUS_COMPONENT_PATH": (
            f"{ctx.home / '.local/share/ibus/component'}:/usr/share/ibus/component"
        )
    }
    run_command(ctx, ["ibus", "write-cache"], as_user=True, env=ibus_env)
    run_command(ctx, ["ibus-daemon", "-drx", "-r", "-t", "refresh"], as_user=True, env=ibus_env)


def reload_systemd_user(ctx: InstallContext) -> None:
    """Reload, enable, and restart the KDictate user service."""

    log("Reloading the user systemd manager")
    run_command(ctx, ["systemctl", "--user", "daemon-reload"], as_user=True)
    run_command(ctx, ["systemctl", "--user", "enable", SERVICE_NAME], as_user=True)
    run_command(ctx, ["systemctl", "--user", "restart", SERVICE_NAME], as_user=True)


def print_summary(ctx: InstallContext) -> None:
    """Print the install result summary."""

    print()
    print("Done.")
    print(f"  Systemd user service: {SERVICE_NAME}")
    print(f"  D-Bus activation name: {DBUS_INTERFACE}")
    print(f"  IBus component metadata: {IBUS_COMPONENT_NAME}")
    print(f"  IBus environment file: {ctx.home / '.config/environment.d' / IBUS_ENV_FILE_NAME}")
    print(f"  Plasma env cleanup: {ctx.home / '.config/plasma-workspace/env' / PLASMA_ENV_SCRIPT_NAME}")
    print(f"  IBus engine executable: {ctx.engine_exec}")
    print(f"  KDE shortcut launcher: {ctx.home / '.local/share/applications' / TOGGLE_DESKTOP_NAME}")
    print()
    print("Select the KDictate engine from IBus after the frontend is installed.")
    print("On KDE Wayland, the installer also selects IBus Wayland as the virtual keyboard when KDE tools are available.")
    print("The installer refreshes the IBus cache and restarts ibus-daemon for the current session.")
    print("After the first install on KDE Wayland, sign out and back in once so KWin picks up the new input-method configuration.")
    print("The core daemon now stays on the transcription side of the boundary only.")


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

    require_command("pacman")
    require_command("python3")
    require_command("systemctl")
    # gdbus is invoked at runtime by the toggle .desktop shortcut, not by the
    # installer. Check it here so a missing gdbus is surfaced at install time
    # rather than silently failing on first Ctrl+Space toggle.
    require_command("gdbus")
    require_command("rsync")

    log("Installing required system package: ibus")
    run_command(ctx, ["pacman", "-S", "--noconfirm", "--needed", "ibus"])

    # Check after pacman — dconf may arrive as an ibus dependency on fresh systems.
    require_command("dconf")

    require_command("ibus")
    require_command("ibus-daemon")

    sync_runtime(ctx)
    # Model is expected at $RUNTIME_DIR/whisper-large-v3-turbo-ct2/.
    install_python_environment(ctx)

    log("Installing systemd user service")
    install_rendered_file(
        ctx,
        ctx.script_dir / "packaging" / "kdictate-systemd.service",
        ctx.home / ".config/systemd/user" / SERVICE_NAME,
    )

    log("Installing D-Bus activation service")
    install_rendered_file(
        ctx,
        ctx.script_dir / "packaging" / f"{APP_ROOT_ID}.service",
        ctx.home / ".local/share/dbus-1/services" / DBUS_SERVICE_NAME,
    )

    log("Installing IBus component metadata")
    install_rendered_file(
        ctx,
        ctx.script_dir / "packaging" / IBUS_COMPONENT_NAME,
        ctx.home / ".local/share/ibus/component" / IBUS_COMPONENT_NAME,
    )

    log("Installing IBus component-path environment")
    install_rendered_file(
        ctx,
        ctx.script_dir / "packaging" / IBUS_ENV_FILE_NAME,
        ctx.home / ".config/environment.d" / IBUS_ENV_FILE_NAME,
    )

    log("Installing Plasma Wayland environment cleanup")
    copy_owned_file(
        ctx,
        ctx.script_dir / "packaging" / PLASMA_ENV_SCRIPT_NAME,
        ctx.home / ".config/plasma-workspace/env" / PLASMA_ENV_SCRIPT_NAME,
    )

    log("Installing KDE shortcut launcher")
    install_rendered_file(
        ctx,
        ctx.script_dir / "packaging" / TOGGLE_DESKTOP_NAME,
        ctx.home / ".local/share/applications" / TOGGLE_DESKTOP_NAME,
    )

    if shutil.which("kbuildsycoca6") is not None:
        run_command(ctx, ["kbuildsycoca6", "--noincremental"], as_user=True, check=False)

    configure_preload_engines(ctx)
    configure_kwin_input_method(ctx)
    refresh_ibus_registry(ctx)
    reload_systemd_user(ctx)
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
