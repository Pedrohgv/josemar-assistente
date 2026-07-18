from __future__ import annotations

import os
import shutil
import subprocess
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAPTOP_LINUX = os.path.join(REPO_ROOT, "laptop", "linux")
LAUNCHER = os.path.join(LAPTOP_LINUX, "josemar-browser-control")
INSTALLER = os.path.join(LAPTOP_LINUX, "install-launcher.sh")
DESKTOP_IN = os.path.join(LAPTOP_LINUX, "josemar-browser.desktop.in")
# Test sandbox roots live under the repo dump_folder (git-ignored) per repo
# sandbox rules; never use tempfile defaults outside the repo.
DUMP_ROOT = os.path.join(REPO_ROOT, "dump_folder", "laptop-launcher-tests")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def strip_comments(text: str) -> str:
    """Strip shell comment lines (lines whose first non-space char is #)."""
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


class LauncherScriptContractTests(unittest.TestCase):
    def test_launcher_files_exist(self) -> None:
        for path in (LAUNCHER, INSTALLER, DESKTOP_IN):
            with self.subTest(path=path):
                self.assertTrue(os.path.isfile(path), f"missing {path}")

    def test_launcher_is_executable(self) -> None:
        self.assertTrue(os.access(LAUNCHER, os.X_OK), "launcher script must be executable")
        self.assertTrue(os.access(INSTALLER, os.X_OK), "installer script must be executable")

    def test_launcher_bash_syntax_ok(self) -> None:
        result = subprocess.run(["bash", "-n", LAUNCHER], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"bash -n failed:\n{result.stderr}")

    def test_launcher_strict_mode(self) -> None:
        text = read(LAUNCHER)
        self.assertIn("set -euo pipefail", text)

    def test_launcher_constants(self) -> None:
        text = read(LAUNCHER)
        # Fixed constants (server-side ports not configurable here).
        self.assertIn('SSH_PORT=2222', text)
        self.assertIn('CDP_PORT=9222', text)
        self.assertIn('SSH_USER="tunnel"', text)
        self.assertIn('DEFAULT_SERVER="josemar-server"', text)

    def test_launcher_no_server_port_env_overrides(self) -> None:
        text = read(LAUNCHER)
        # Must not reintroduce server-side configurable ports.
        self.assertNotIn("BROWSER_TUNNEL_SSH_PORT", text)
        self.assertNotIn("BROWSER_TUNNEL_CDP_PORT", text)
        self.assertNotIn("BROWSER_TUNNEL_USER", text)

    def test_launcher_default_paths(self) -> None:
        text = read(LAUNCHER)
        self.assertIn(".josemar-chrome-profile", text)
        self.assertIn(".ssh/josemar_browser_tunnel", text)
        self.assertIn(".ssh/josemar_browser_tunnel_known_hosts", text)

    def test_launcher_chrome_detection(self) -> None:
        text = read(LAUNCHER)
        for c in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            with self.subTest(chrome=c):
                self.assertIn(c, text)

    def test_launcher_ssh_flags(self) -> None:
        text = read(LAUNCHER)
        self.assertIn("IdentitiesOnly=yes", text)
        self.assertIn("StrictHostKeyChecking=accept-new", text)
        self.assertIn("UserKnownHostsFile=", text)
        self.assertIn("ServerAliveInterval=30", text)
        self.assertIn("ServerAliveCountMax=3", text)
        self.assertIn("ExitOnForwardFailure=yes", text)
        self.assertIn("ControlMaster=yes", text)
        self.assertIn("ControlPath=", text)
        # Exact reverse forward to loopback CDP port.
        self.assertIn('127.0.0.1:${CDP_PORT}:127.0.0.1:${CDP_PORT}', text)

    def test_launcher_ssh_control_uses_O_exit_not_O_stop(self) -> None:
        text = read(LAUNCHER)
        # -O exit actually terminates the reverse-forward master; -O stop can
        # leave it alive.
        self.assertIn("-O exit", text)
        self.assertNotIn("-O stop", text)

    def test_launcher_ssh_control_includes_port_and_server(self) -> None:
        text = read(LAUNCHER)
        # -O check and -O exit must include the fixed port and server identity
        # consistently with the connect command.
        # Count occurrences of -p "$SSH_PORT" in control contexts; at minimum in
        # the exit and check calls.
        self.assertGreaterEqual(text.count('-p "$SSH_PORT" "${SSH_USER}@${SERVER}"'), 3)

    def test_launcher_ssh_pid_extraction_captures_stderr(self) -> None:
        text = read(LAUNCHER)
        # OpenSSH emits `Master running (pid=...)` on stderr from `ssh -O check`.
        # The PID-recording block must capture combined stdout+stderr (2>&1),
        # not discard stderr (2>/dev/null), and must not log the output.
        self.assertIn("2>&1", text)
        # The PID-recording block must not pipe stderr to /dev/null.
        # Find the -O check block and assert it uses 2>&1, not 2>/dev/null.
        self.assertIn("ssh -O check", text)
        # Bash regex parse (no sed/head dependency for PID extraction).
        self.assertIn("BASH_REMATCH", text)
        # Must not log the captured check output.
        self.assertNotIn('log "$check_out"', text)

    def test_launcher_cdp_check_loopback_only(self) -> None:
        text = read(LAUNCHER)
        self.assertIn("http://127.0.0.1:${CDP_PORT}/json/version", text)
        # Must not bind CDP on 0.0.0.0.
        self.assertIn("--remote-debugging-address=127.0.0.1", text)
        self.assertNotIn("--remote-debugging-address=0.0.0.0", text)

    def test_launcher_dedicated_profile_flag(self) -> None:
        text = read(LAUNCHER)
        self.assertIn("--user-data-dir=\"$PROFILE_DIR\"", text)

    def test_launcher_disable_background_mode(self) -> None:
        text = read(LAUNCHER)
        # --disable-background-mode prevents Chrome from lingering after the
        # last dedicated-profile window closes.
        self.assertIn("--disable-background-mode", text)
        # The inaccurate RendererCodeIntegrity/Mint sandbox comment must be gone.
        self.assertNotIn("RendererCodeIntegrity", text)

    def test_launcher_pid_verification_before_signal(self) -> None:
        text = read(LAUNCHER)
        # Must verify PIDs/cmdlines before signaling (never pkill generic).
        self.assertIn("verify_chrome_pid", text)
        self.assertIn("verify_ssh_pid", text)
        self.assertIn("/proc/", text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("killall", text)

    def test_launcher_no_sudo_no_pkg_manager(self) -> None:
        text = strip_comments(read(LAUNCHER))
        self.assertNotIn("sudo", text)
        self.assertNotIn("apt-get", text)
        self.assertNotIn("apt install", text)
        self.assertNotIn("dnf", text)
        self.assertNotIn("pacman", text)

    def test_launcher_no_autostart(self) -> None:
        text = strip_comments(read(LAUNCHER))
        self.assertNotIn("systemctl --user enable", text)
        self.assertNotIn("autostart", text.lower())
        self.assertNotIn(".config/autostart", text)

    def test_launcher_no_key_contents_read(self) -> None:
        text = read(LAUNCHER)
        # Must not cat/print the key file contents.
        self.assertNotIn("cat \"$SSH_KEY\"", text)
        self.assertNotIn("cat $SSH_KEY", text)
        self.assertNotIn("cat \"$SSH_KEY.pub\"", text)

    def test_launcher_actions(self) -> None:
        text = read(LAUNCHER)
        self.assertIn("do_start", text)
        self.assertIn("do_stop", text)
        self.assertIn("do_status", text)
        self.assertIn("do_stop_quiet", text)

    def test_launcher_notify_fallback(self) -> None:
        text = read(LAUNCHER)
        self.assertIn("notify-send", text)
        # Graceful fallback: notify-send is checked with command -v.
        self.assertIn("command -v notify-send", text)

    def test_launcher_locking(self) -> None:
        text = read(LAUNCHER)
        self.assertIn("acquire_lock", text)
        self.assertIn("release_lock", text)
        self.assertIn("LOCK_FILE", text)

    def test_launcher_owner_cleanup_trap(self) -> None:
        text = read(LAUNCHER)
        # owner_cleanup checks the lock holds our PID before tearing down, so
        # a non-owner process cannot kill the active controller's processes.
        self.assertIn("owner_cleanup", text)
        # Ownership check: compares the lock's PID to $$ (this process).
        self.assertIn('"$lock_pid" = "$$"', text)
        # Traps on EXIT/TERM/INT/HUP after lock acquisition.
        self.assertIn("trap 'owner_cleanup' EXIT", text)
        self.assertIn("trap 'owner_cleanup; exit 130' INT", text)
        self.assertIn("trap 'owner_cleanup; exit 143' TERM", text)
        self.assertIn("trap 'owner_cleanup; exit 129' HUP", text)

    def test_launcher_no_O_stop(self) -> None:
        text = read(LAUNCHER)
        # Ensure no -O stop remains anywhere (replaced by -O exit).
        self.assertNotIn("-O stop", text)


class DesktopTemplateContractTests(unittest.TestCase):
    def test_desktop_template_basics(self) -> None:
        text = read(DESKTOP_IN)
        self.assertIn("Type=Application", text)
        self.assertIn("Name=Josemar Browser", text)
        self.assertIn("Terminal=false", text)
        self.assertIn("Exec=__INSTALL_BIN__ start", text)
        self.assertIn("Icon=google-chrome", text)
        self.assertIn("StartupNotify=true", text)

    def test_desktop_template_stop_action(self) -> None:
        text = read(DESKTOP_IN)
        self.assertIn("Actions=Stop;", text)
        self.assertIn("[Desktop Action Stop]", text)
        self.assertIn("Name=Stop Josemar Browser", text)
        self.assertIn("Exec=__INSTALL_BIN__ stop", text)

    def test_desktop_template_managed_marker(self) -> None:
        text = read(DESKTOP_IN)
        # Ownership marker so the installer can detect its own desktop entry.
        self.assertIn("X-Josemar-Managed=true", text)

    def test_desktop_template_stop_icon_process_stop(self) -> None:
        text = read(DESKTOP_IN)
        # Semantic stop icon for the Stop action.
        self.assertIn("Icon=process-stop", text)

    def test_desktop_template_no_autostart(self) -> None:
        text = read(DESKTOP_IN)
        self.assertNotIn("X-GNOME-Autostart-enabled", text)
        self.assertNotIn("Autostart", text)


class InstallerScriptContractTests(unittest.TestCase):
    def test_installer_bash_syntax_ok(self) -> None:
        result = subprocess.run(["bash", "-n", INSTALLER], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"bash -n failed:\n{result.stderr}")

    def test_installer_strict_mode(self) -> None:
        text = read(INSTALLER)
        self.assertIn("set -euo pipefail", text)

    def test_installer_user_level_only(self) -> None:
        text = strip_comments(read(INSTALLER))
        self.assertIn(".local/bin", text)
        self.assertIn(".local/share/applications", text)
        # No system-level paths in executable code.
        self.assertNotIn("/usr/bin", text)
        self.assertNotIn("/usr/share/applications", text)

    def test_installer_no_sudo_no_pkg_manager(self) -> None:
        text = strip_comments(read(INSTALLER))
        self.assertNotIn("sudo", text)
        self.assertNotIn("apt-get", text)
        self.assertNotIn("apt install", text)

    def test_installer_no_autostart(self) -> None:
        text = strip_comments(read(INSTALLER))
        # Must not configure autostart (no autostart dir creation, no enable).
        self.assertNotIn("systemctl --user enable", text)
        self.assertNotIn(".config/autostart", text)
        self.assertNotIn("mkdir -p $HOME/.config/autostart", text)
        self.assertNotIn('mkdir -p "$HOME/.config/autostart"', text)

    def test_installer_uninstall_preserves_profile_and_key(self) -> None:
        text = read(INSTALLER)
        self.assertIn("--uninstall", text)
        # Uninstall must not remove profile/key/known_hosts/credentials.
        self.assertIn("Preserved", text)
        self.assertIn("Chrome profile", text)
        self.assertIn("SSH key", text)
        self.assertIn("known_hosts", text)

    def test_installer_does_not_launch(self) -> None:
        text = read(INSTALLER)
        # Must not invoke the launcher's start action during install.
        self.assertNotIn("josemar-browser-control start", text)
        self.assertNotIn("$INSTALLED_BIN start", text)

    def test_installer_renders_exec_path(self) -> None:
        text = read(INSTALLER)
        self.assertIn("__INSTALL_BIN__", text)
        self.assertIn("sed", text)

    def test_installer_ownership_marker_checks(self) -> None:
        text = read(INSTALLER)
        # Must verify symlink target / managed marker before replacing or
        # uninstalling, and fail safely on foreign files.
        self.assertIn("is_owned_symlink", text)
        self.assertIn("is_managed_desktop", text)
        self.assertIn("X-Josemar-Managed=true", text)
        self.assertIn("foreign file", text)

    def test_installer_no_blind_overwrite(self) -> None:
        text = read(INSTALLER)
        # Must not rm -f a foreign file blindly; the die() on foreign file is
        # the safe path.
        self.assertIn("refusing to overwrite", text)


class InstallerRuntimeTests(unittest.TestCase):
    """Run the installer in an isolated fake HOME/XDG tree under repo dump_folder."""

    def setUp(self) -> None:
        if not shutil.which("bash"):
            self.skipTest("bash not available")
        os.makedirs(DUMP_ROOT, exist_ok=True)
        self.fake_root = os.path.join(DUMP_ROOT, f"run-{os.getpid()}-{id(self):x}")
        self.fake_home = os.path.join(self.fake_root, "home", "testuser")
        os.makedirs(self.fake_home)
        # Pre-create the fake profile and SSH key to verify uninstall preserves them.
        self.fake_profile = os.path.join(self.fake_home, ".josemar-chrome-profile")
        self.fake_key = os.path.join(self.fake_home, ".ssh", "josemar_browser_tunnel")
        self.fake_known_hosts = os.path.join(self.fake_home, ".ssh", "josemar_browser_tunnel_known_hosts")
        os.makedirs(self.fake_profile, exist_ok=True)
        os.makedirs(os.path.dirname(self.fake_key), exist_ok=True)
        with open(self.fake_key, "w") as fh:
            fh.write("FAKE-PRIVATE-KEY-CONTENTS\n")
        os.chmod(self.fake_key, 0o600)
        with open(self.fake_known_hosts, "w") as fh:
            fh.write("FAKE-KNOWN-HOSTS\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.fake_root, ignore_errors=True)

    def _run_installer(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": self.fake_home,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "XDG_DATA_HOME": os.path.join(self.fake_home, ".local", "share"),
            "XDG_RUNTIME_DIR": os.path.join(self.fake_root, "run"),
        }
        os.makedirs(env["XDG_RUNTIME_DIR"], exist_ok=True)
        return subprocess.run(
            ["bash", INSTALLER, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_install_creates_user_level_files(self) -> None:
        result = self._run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        bin_path = os.path.join(self.fake_home, ".local", "bin", "josemar-browser-control")
        desktop_path = os.path.join(self.fake_home, ".local", "share", "applications", "josemar-browser.desktop")
        self.assertTrue(os.path.islink(bin_path), f"expected symlink at {bin_path}")
        self.assertTrue(os.path.isfile(desktop_path), f"expected desktop entry at {desktop_path}")

    def test_installed_desktop_entry_renders_exec_path(self) -> None:
        self._run_installer()
        desktop_path = os.path.join(self.fake_home, ".local", "share", "applications", "josemar-browser.desktop")
        text = read(desktop_path)
        expected_exec = os.path.join(self.fake_home, ".local", "bin", "josemar-browser-control")
        self.assertIn(f"Exec={expected_exec} start", text)
        self.assertIn(f"Exec={expected_exec} stop", text)
        self.assertIn("Name=Josemar Browser", text)
        self.assertIn("Terminal=false", text)
        # No template placeholder should remain.
        self.assertNotIn("__INSTALL_BIN__", text)

    def test_install_is_idempotent(self) -> None:
        r1 = self._run_installer()
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self._run_installer()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        bin_path = os.path.join(self.fake_home, ".local", "bin", "josemar-browser-control")
        desktop_path = os.path.join(self.fake_home, ".local", "share", "applications", "josemar-browser.desktop")
        self.assertTrue(os.path.islink(bin_path))
        self.assertTrue(os.path.isfile(desktop_path))

    def test_install_does_not_create_autostart(self) -> None:
        self._run_installer()
        autostart_dir = os.path.join(self.fake_home, ".config", "autostart")
        self.assertFalse(os.path.exists(autostart_dir), "installer must not create autostart dir")

    def test_install_preserves_fake_profile_and_key(self) -> None:
        self._run_installer()
        self.assertTrue(os.path.isdir(self.fake_profile), "profile must be preserved by install")
        self.assertTrue(os.path.isfile(self.fake_key), "key must be preserved by install")
        with open(self.fake_key) as fh:
            self.assertIn("FAKE-PRIVATE-KEY-CONTENTS", fh.read())

    def test_uninstall_removes_only_owned_files(self) -> None:
        self._run_installer()
        r = self._run_installer("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        bin_path = os.path.join(self.fake_home, ".local", "bin", "josemar-browser-control")
        desktop_path = os.path.join(self.fake_home, ".local", "share", "applications", "josemar-browser.desktop")
        self.assertFalse(os.path.exists(bin_path), "uninstall must remove the launcher symlink")
        self.assertFalse(os.path.exists(desktop_path), "uninstall must remove the desktop entry")

    def test_uninstall_preserves_profile_key_and_known_hosts(self) -> None:
        self._run_installer()
        self._run_installer("--uninstall")
        self.assertTrue(os.path.isdir(self.fake_profile), "uninstall must NOT remove Chrome profile")
        self.assertTrue(os.path.isfile(self.fake_key), "uninstall must NOT remove SSH key")
        self.assertTrue(os.path.isfile(self.fake_known_hosts), "uninstall must NOT remove known_hosts")
        with open(self.fake_key) as fh:
            self.assertIn("FAKE-PRIVATE-KEY-CONTENTS", fh.read())

    def test_desktop_entry_validates(self) -> None:
        if not shutil.which("desktop-file-validate"):
            self.skipTest("desktop-file-validate not installed")
        self._run_installer()
        desktop_path = os.path.join(self.fake_home, ".local", "share", "applications", "josemar-browser.desktop")
        result = subprocess.run(["desktop-file-validate", desktop_path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"desktop-file-validate failed:\n{result.stderr}")

    def test_install_fails_on_foreign_bin(self) -> None:
        # Pre-create a foreign regular file at the bin path; install must fail.
        bin_path = os.path.join(self.fake_home, ".local", "bin", "josemar-browser-control")
        os.makedirs(os.path.dirname(bin_path), exist_ok=True)
        with open(bin_path, "w") as fh:
            fh.write("#!/bin/sh\necho foreign\n")
        os.chmod(bin_path, 0o755)
        result = self._run_installer()
        self.assertNotEqual(result.returncode, 0, "install must fail on foreign bin file")
        # Foreign file must be preserved.
        self.assertTrue(os.path.isfile(bin_path), "foreign bin file must be preserved")
        with open(bin_path) as fh:
            self.assertIn("foreign", fh.read())

    def test_install_fails_on_foreign_desktop(self) -> None:
        # Pre-create a foreign desktop entry without the managed marker.
        desktop_path = os.path.join(self.fake_home, ".local", "share", "applications", "josemar-browser.desktop")
        os.makedirs(os.path.dirname(desktop_path), exist_ok=True)
        with open(desktop_path, "w") as fh:
            fh.write("[Desktop Entry]\nType=Application\nName=Foreign\nExec=false\n")
        result = self._run_installer()
        self.assertNotEqual(result.returncode, 0, "install must fail on foreign desktop file")
        # Foreign file must be preserved.
        self.assertTrue(os.path.isfile(desktop_path), "foreign desktop file must be preserved")
        with open(desktop_path) as fh:
            self.assertIn("Foreign", fh.read())

    def test_install_preflight_no_partial_install_on_foreign_desktop(self) -> None:
        # bin absent, desktop foreign: installer must fail preflight and must
        # NOT create the bin symlink (no partial install).
        bin_path = os.path.join(self.fake_home, ".local", "bin", "josemar-browser-control")
        desktop_path = os.path.join(self.fake_home, ".local", "share", "applications", "josemar-browser.desktop")
        os.makedirs(os.path.dirname(desktop_path), exist_ok=True)
        with open(desktop_path, "w") as fh:
            fh.write("[Desktop Entry]\nType=Application\nName=Foreign\nExec=false\n")
        result = self._run_installer()
        self.assertNotEqual(result.returncode, 0, "install must fail on foreign desktop file")
        # Foreign desktop must be preserved.
        self.assertTrue(os.path.isfile(desktop_path), "foreign desktop file must be preserved")
        with open(desktop_path) as fh:
            self.assertIn("Foreign", fh.read())
        # No bin symlink must be created (preflight prevents partial install).
        self.assertFalse(
            os.path.exists(bin_path) or os.path.islink(bin_path),
            "installer must not create bin symlink when desktop preflight fails",
        )

    def test_uninstall_preserves_foreign_bin_and_desktop(self) -> None:
        # Pre-create foreign files; uninstall must not remove them.
        bin_path = os.path.join(self.fake_home, ".local", "bin", "josemar-browser-control")
        desktop_path = os.path.join(self.fake_home, ".local", "share", "applications", "josemar-browser.desktop")
        os.makedirs(os.path.dirname(bin_path), exist_ok=True)
        os.makedirs(os.path.dirname(desktop_path), exist_ok=True)
        with open(bin_path, "w") as fh:
            fh.write("#!/bin/sh\necho foreign\n")
        os.chmod(bin_path, 0o755)
        with open(desktop_path, "w") as fh:
            fh.write("[Desktop Entry]\nType=Application\nName=Foreign\nExec=false\n")
        result = self._run_installer("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Foreign files must be preserved.
        self.assertTrue(os.path.isfile(bin_path), "foreign bin must be preserved by uninstall")
        self.assertTrue(os.path.isfile(desktop_path), "foreign desktop must be preserved by uninstall")
        with open(bin_path) as fh:
            self.assertIn("foreign", fh.read())

    def test_installed_desktop_has_managed_marker(self) -> None:
        self._run_installer()
        desktop_path = os.path.join(self.fake_home, ".local", "share", "applications", "josemar-browser.desktop")
        text = read(desktop_path)
        self.assertIn("X-Josemar-Managed=true", text)

    def test_installed_bin_is_symlink_to_repo_launcher(self) -> None:
        self._run_installer()
        bin_path = os.path.join(self.fake_home, ".local", "bin", "josemar-browser-control")
        self.assertTrue(os.path.islink(bin_path))
        target = os.readlink(bin_path)
        # Resolve to an absolute path and compare to the repo launcher.
        resolved = os.path.realpath(bin_path)
        self.assertEqual(resolved, LAUNCHER)


if __name__ == "__main__":
    unittest.main()