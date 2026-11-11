import io
import json
import os
from pathlib import Path
import pty
import select
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ush"))

import download_pygraf_graphics as dl
import ssh_helper


def _spawn_interactive_bash_pty():
    """Spawns a local interactive bash session attached to a clean PTY."""
    master, slave = pty.openpty()
    ssh_helper._set_pty_winsize(master, rows=24, cols=32768)
    ssh_helper._set_pty_winsize(slave, rows=24, cols=32768)
    pid = os.fork()
    if pid == 0:
        os.close(master)
        os.setsid()
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        if slave > 2:
            os.close(slave)
        os.execlp("bash", "bash", "--noprofile", "--norc", "-i")
    os.close(slave)
    os.set_blocking(master, False)

    # Wait briefly and disable echo / prompts like run_worker_server does
    time.sleep(0.15)
    ssh_helper._drain_pty(master, duration=0.1)
    ssh_helper._write_pty_nonblocking(
        master,
        b"stty -echo 2>/dev/null || true; PS1=''; PS2=''; unset PROMPT_COMMAND 2>/dev/null || true\n",
        idle_timeout=2.0,
    )
    ssh_helper._drain_pty(master, duration=0.1)
    return master, pid


class SshHelperPtyAndStreamingTests(unittest.TestCase):
    def test_pty_streaming_delivers_chunks_incrementally(self):
        master, pid = _spawn_interactive_bash_pty()
        events = []
        try:
            # Exceed stdio 4KB-8KB base64 output buffer on chunk 1 so chunk 1 arrives before sleep ends
            cmd = "printf 'PART1_%-8192s\\n' X; sleep 0.6; printf 'PART2_%-8192s\\n' Y"
            rc = ssh_helper._execute_on_pty_stream(
                master,
                cmd=cmd,
                idle_timeout=5.0,
                on_stdout=lambda chunk: events.append((time.monotonic(), chunk)),
            )
            self.assertEqual(rc, 0)
            combined = b"".join(chunk for _, chunk in events)
            self.assertIn(b"PART1_", combined)
            self.assertIn(b"PART2_", combined)

            idx1 = next(i for i, (_, c) in enumerate(events) if b"PART1_" in c)
            idx2 = next(i for i, (_, c) in enumerate(events) if b"PART2_" in c)
            self.assertGreater(events[idx2][0] - events[idx1][0], 0.35)
        finally:
            try:
                os.write(master, b"exit\n")
            except OSError:
                pass
            try:
                os.close(master)
            except OSError:
                pass
            os.waitpid(pid, 0)

    def test_long_command_staging_works_losslessly(self):
        master, pid = _spawn_interactive_bash_pty()
        try:
            # Command > 320 chars exercises chunked cmd.b64 staging path
            long_comment = "# " + ("LONG_COMMAND_PADDING_" * 30)
            cmd = f"{long_comment}\nprintf 'LONG_CMD_OK\\n'"
            rc, out = ssh_helper._execute_on_pty(master, cmd=cmd, timeout=5.0, idle_timeout=5.0)
            self.assertEqual(rc, 0)
            self.assertEqual(out, b"LONG_CMD_OK\n")
        finally:
            try:
                os.write(master, b"exit\n")
            except OSError:
                pass
            try:
                os.close(master)
            except OSError:
                pass
            os.waitpid(pid, 0)

    def test_pty_isolated_stderr_prevents_base64_corruption_in_tar(self):
        master, pid = _spawn_interactive_bash_pty()
        try:
            with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpct_") as tmpdir:
                tmp = Path(tmpdir)
                f1 = tmp / "fig1.png"
                f2 = tmp / "fig2.png"
                f_miss = tmp / "does_not_exist.png"
                f1.write_bytes(b"\x89PNG\r\n\x1a\nFIRST_IMAGE_PAYLOAD" * 20)
                f2.write_bytes(b"\x89PNG\r\n\x1a\nSECOND_IMAGE_PAYLOAD" * 20)

                paths = [str(f_miss), str(f1), str(f2)]
                stdin_data = ("\0".join(paths) + "\0").encode("utf-8")

                stdout_chunks = []
                stderr_chunks = []
                rc = ssh_helper._execute_on_pty_stream(
                    master,
                    cmd="tar -P -cf - --null -T -",
                    stdin_bytes=stdin_data,
                    idle_timeout=10.0,
                    on_stdout=stdout_chunks.append,
                    on_stderr=stderr_chunks.append,
                )

                tar_bytes = b"".join(stdout_chunks)
                err_text = b"".join(stderr_chunks).decode("utf-8", errors="ignore")

                self.assertNotEqual(rc, 0)
                self.assertIn("does_not_exist.png", err_text)

                extracted = {}
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tf:
                    for m in tf.getmembers():
                        if m.isfile():
                            extracted[m.name.lstrip("/")] = tf.extractfile(m).read()

                self.assertEqual(extracted[str(f1).lstrip("/")], f1.read_bytes())
                self.assertEqual(extracted[str(f2).lstrip("/")], f2.read_bytes())
                self.assertNotIn(str(f_miss).lstrip("/"), extracted)
        finally:
            try:
                os.write(master, b"exit\n")
            except OSError:
                pass
            try:
                os.close(master)
            except OSError:
                pass
            os.waitpid(pid, 0)

    def test_nonblocking_write_times_out_when_pty_stalled(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Shrink send buffer so kernel write blocks quickly without peer reading
            left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            left.setblocking(False)
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                ssh_helper._write_pty_nonblocking(
                    left.fileno(),
                    data=b"X" * 2000000,
                    idle_timeout=0.3,
                    deadline=t0 + 0.5,
                )
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 2.0)
        finally:
            left.close()
            right.close()

    def test_ping_worker_session_detects_wedged_socket(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpct_") as tmpdir:
            sock_path = os.path.join(tmpdir, "wedged.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            srv.listen(4)
            stop_evt = threading.Event()
            accepted = []

            def _accept_and_hang():
                while not stop_evt.is_set():
                    r, _, _ = select.select([srv], [], [], 0.05)
                    if srv in r:
                        conn, _ = srv.accept()
                        accepted.append(conn)

            th = threading.Thread(target=_accept_and_hang, daemon=True)
            th.start()
            try:
                # Raw connect succeeds, but ping detects the wedged server!
                self.assertTrue(ssh_helper.is_socket_alive(sock_path))
                self.assertFalse(ssh_helper.ping_worker_session(sock_path, timeout=0.4, probe_remote=True))
            finally:
                stop_evt.set()
                th.join(timeout=1.0)
                for c in accepted:
                    try:
                        c.close()
                    except OSError:
                        pass
                srv.close()

    def test_non_tty_cmd_fails_fast_without_prompting(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpct_") as tmpdir:
            tmp = Path(tmpdir)
            fake_home = tmp / "home"
            fake_home.mkdir()
            env = os.environ.copy()
            env["HOME"] = str(fake_home)
            # Point worker path lookup to unused cluster/user via isolated user flag
            res = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "ush" / "ssh_helper.py"),
                    "unlikely_test_user_999",
                    "gaeac7",
                    "--config-file",
                    str(tmp / ".hpc_config"),
                    "--cmd",
                    "echo hi",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                timeout=5.0,
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("No active HPC session", res.stderr)


class WorkerServerIntegrationTests(unittest.TestCase):
    def _create_fake_ssh_bin(self, bin_dir: Path, mode: str = "direct") -> None:
        ssh_script = bin_dir / "ssh"
        if mode == "bastion":
            ssh_script.write_text(
                "#!/bin/bash\n"
                "echo 'NOAA RDHPCS Bastion Node'\n"
                "echo 'Hit ^C within 5 seconds to specify a hostname'\n"
                "trap 'GOT_INT=1' INT\n"
                "GOT_INT=0\n"
                "for i in 1 2 3 4 5 6 7 8 9 10; do\n"
                "  if [ \"$GOT_INT\" = \"1\" ]; then break; fi\n"
                "  sleep 0.1\n"
                "done\n"
                "trap - INT\n"
                "printf 'Enter a hostname > '\n"
                "IFS= read -r host_choice\n"
                "if [ \"$host_choice\" != \"gaea71\" ]; then\n"
                "  echo \"Unexpected host choice: $host_choice\" >&2\n"
                "  exit 1\n"
                "fi\n"
                "exec bash --noprofile --norc -i\n",
                encoding="utf-8",
            )
        else:
            ssh_script.write_text(
                "#!/bin/bash\n"
                "exec bash --noprofile --norc -i\n",
                encoding="utf-8",
            )
        os.chmod(ssh_script, 0o755)

    def test_worker_server_bastion_handshake_disconnect_recovery_and_944_tar_pull(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpct_") as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            self._create_fake_ssh_bin(bin_dir, mode="bastion")

            sock_path = str(tmp / "worker_test.sock")
            fake_master = str(tmp / "master.sock")
            Path(fake_master).touch()

            old_path = os.environ.get("PATH", "")
            env_patch = {"PATH": f"{bin_dir}:{old_path}"}
            server_proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "ush" / "ssh_helper.py"),
                    "--worker-daemon",
                    "--sock-path", sock_path,
                    "--master-sock", fake_master,
                    "--worker-user", "tester",
                    "--worker-host", "gaea-c7.princeton.rdhpcs.noaa.gov",
                    "--default-node", "gaea71",
                    "--idle-timeout", "0",
                ],
                env={**os.environ, **env_patch},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if ssh_helper.ping_worker_session(sock_path, timeout=1.0, probe_remote=True):
                        break
                    time.sleep(0.1)
                self.assertTrue(
                    ssh_helper.ping_worker_session(sock_path, timeout=2.0, probe_remote=True),
                    f"Worker failed startup; err sidecar: {Path(ssh_helper.get_worker_err_path(sock_path)).read_text() if os.path.exists(ssh_helper.get_worker_err_path(sock_path)) else ''}",
                )
                self.assertTrue(os.path.exists(ssh_helper.get_worker_pid_path(sock_path)))

                # 1. Test client disconnect (Ctrl+C simulation) mid-stream and immediate recovery
                req = json.dumps({"cmd": "yes SPAM", "stream": True, "timeout": 0, "idle_timeout": 10.0}).encode() + b"\n"
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(3.0)
                    s.connect(sock_path)
                    s.sendall(req)
                    first_chunk = s.recv(4096)
                    self.assertTrue(len(first_chunk) > 0)
                # Abruptly closed `s`! Verify worker recovers shell and executes next command cleanly.
                with patch.object(ssh_helper, "find_active_worker_session", return_value=("tester", sock_path)):
                    rc, out, err = ssh_helper.run_remote_command(
                        "tester",
                        "gaeac7",
                        "echo RECOVERED_OK",
                        timeout=5.0,
                    )
                self.assertEqual(rc, 0, f"err={err}")
                self.assertEqual(out.strip(), b"RECOVERED_OK")

                # 2. Test 944-path tar pull (gzipped stdin + missing files + existing files)
                rem_root = tmp / "remote_figs"
                rem_root.mkdir()
                loc_root = tmp / "local_figs"
                tasks = []
                for idx in range(944):
                    r_file = rem_root / f"fig_{idx:04d}.png"
                    l_file = loc_root / f"fig_{idx:04d}.png"
                    if idx % 2 == 0:
                        r_file.write_bytes(f"PNG_BODY_{idx:04d}".encode("ascii"))
                    tasks.append((str(r_file), l_file))

                # Point _resolve_ssh_helper_path & find_active_worker_session cleanly by naming real socket path?
                # Wait: stream_tar_pull spawns ssh_helper.py gaeac7 -u tester --cmd ..., which uses /tmp/hpc_term_tester_gaeac7.sock
                # Let's test both run_remote_command with 944 paths directly AND via symlinked sock or stream_tar_pull!
                with patch.object(ssh_helper, "find_active_worker_session", return_value=("tester", sock_path)):
                    path_data = ("\0".join(r for r, _ in tasks) + "\0").encode("utf-8")
                    out_tar = bytearray()
                    err_tar = bytearray()
                    rc_tar = ssh_helper.run_remote_command(
                        "tester",
                        "gaeac7",
                        "tar -P -cf - --null -T -",
                        stdin_bytes=path_data,
                        timeout=30.0,
                        idle_timeout=10.0,
                        on_stdout=out_tar.extend,
                        on_stderr=err_tar.extend,
                    )[0]
                self.assertNotEqual(rc_tar, 0)
                extracted = {}
                with tarfile.open(fileobj=io.BytesIO(bytes(out_tar)), mode="r|*") as tf:
                    for m in tf:
                        if m.isfile() and not os.path.basename(m.name).startswith("._"):
                            extracted[m.name.lstrip("/")] = tf.extractfile(m).read()
                self.assertEqual(len(extracted), 472)
                self.assertEqual(extracted[str(rem_root / "fig_0000.png").lstrip("/")], b"PNG_BODY_0000")
                self.assertEqual(extracted[str(rem_root / "fig_0942.png").lstrip("/")], b"PNG_BODY_0942")
                self.assertIn("fig_0001.png", err_tar.decode("utf-8", errors="ignore"))

                # Clean exit
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(3.0)
                    s.connect(sock_path)
                    s.sendall(b'{"cmd": "exit"}\n')
                    resp = json.loads(s.recv(1024).decode("utf-8"))
                    self.assertEqual(resp["code"], 0)
                server_proc.wait(timeout=5.0)
                self.assertFalse(os.path.exists(sock_path))
                self.assertFalse(os.path.exists(ssh_helper.get_worker_pid_path(sock_path)))
            finally:
                if server_proc.poll() is None:
                    server_proc.terminate()
                    server_proc.wait(timeout=3.0)
                for pipe in (server_proc.stdout, server_proc.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass

    def test_stream_tar_pull_end_to_end_via_ssh_helper_subprocess(self):
        unique_user = f"e2e_{uuid.uuid4().hex[:8]}"
        sock_path = ssh_helper.get_worker_sock_path(unique_user, "gaeac7")

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpce2e_") as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            self._create_fake_ssh_bin(bin_dir, mode="bastion")

            fake_master = str(tmp / "master.sock")
            Path(fake_master).touch()

            server_proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "ush" / "ssh_helper.py"),
                    "--worker-daemon",
                    "--sock-path", sock_path,
                    "--master-sock", fake_master,
                    "--worker-user", unique_user,
                    "--worker-host", "gaea-c7.princeton.rdhpcs.noaa.gov",
                    "--default-node", "gaea71",
                    "--idle-timeout", "0",
                ],
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if dl.test_hpc_session_ready("gaeac7", username=unique_user):
                        break
                    time.sleep(0.1)
                self.assertTrue(dl.test_hpc_session_ready("gaeac7", username=unique_user))

                rem_root = tmp / "remote"
                rem_root.mkdir()
                loc_root = tmp / "local"
                tasks = []
                for idx in range(944):
                    r_file = rem_root / f"fig_{idx:04d}.png"
                    l_file = loc_root / f"fig_{idx:04d}.png"
                    if idx % 2 == 0:
                        r_file.write_bytes(f"PNG_E2E_{idx:04d}".encode("ascii"))
                    tasks.append((str(r_file), l_file))

                completed, missing, errors = dl.stream_tar_pull(
                    username=unique_user,
                    tasks=tasks,
                    cluster="gaeac7",
                    debug=False,
                )
                self.assertEqual((completed, missing, errors), (472, 472, 0))
                self.assertEqual((loc_root / "fig_0000.png").read_bytes(), b"PNG_E2E_0000")
                self.assertEqual((loc_root / "fig_0942.png").read_bytes(), b"PNG_E2E_0942")
                self.assertFalse((loc_root / "fig_0001.png").exists())
            finally:
                if server_proc.poll() is None:
                    server_proc.terminate()
                    server_proc.wait(timeout=3.0)
                for pipe in (server_proc.stdout, server_proc.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass
                for extra in (
                    sock_path,
                    ssh_helper.get_worker_pid_path(sock_path),
                    ssh_helper.get_worker_err_path(sock_path),
                ):
                    if os.path.exists(extra):
                        try:
                            os.unlink(extra)
                        except OSError:
                            pass

    def test_stop_cleans_legacy_wedged_worker_without_pid_file(self):
        unique_user = f"legacy_{uuid.uuid4().hex[:8]}"
        sock_path = ssh_helper.get_worker_sock_path(unique_user, "gaeac7")

        # Create a socket and launch a sleeping worker-like process with NO .pid file
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(1)
        dummy_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                "--worker-daemon",
                "--sock-path",
                sock_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            pids = ssh_helper._find_worker_pids(sock_path)
            self.assertIn(dummy_proc.pid, pids)
            self.assertFalse(os.path.exists(ssh_helper.get_worker_pid_path(sock_path)))

            stopped = ssh_helper.stop_background_terminal_session(unique_user, "gaeac7")
            self.assertTrue(stopped)
            self.assertFalse(os.path.exists(sock_path))
            rc = dummy_proc.wait(timeout=3.0)
            self.assertNotEqual(rc, 0)
        finally:
            srv.close()
            if dummy_proc.poll() is None:
                dummy_proc.kill()
                dummy_proc.wait(timeout=2.0)
            if os.path.exists(sock_path):
                os.unlink(sock_path)

    def test_run_worker_server_fails_fast_when_remote_sends_no_bytes(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpcfb_") as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            ssh_script = bin_dir / "ssh"
            ssh_script.write_text("#!/bin/bash\nsleep 30\n", encoding="utf-8")
            os.chmod(ssh_script, 0o755)

            sock_path = str(tmp / "silent.sock")
            fake_master = str(tmp / "master.sock")
            Path(fake_master).touch()

            t0 = time.monotonic()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "ush" / "ssh_helper.py"),
                    "--worker-daemon",
                    "--sock-path", sock_path,
                    "--master-sock", fake_master,
                    "--worker-user", "tester",
                    "--worker-host", "gaea-c7.princeton.rdhpcs.noaa.gov",
                    "--first-byte-timeout", "0.5",
                ],
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                rc = proc.wait(timeout=5.0)
                elapsed = time.monotonic() - t0
                self.assertEqual(rc, 0)
                self.assertLess(elapsed, 3.0)
                err_file = Path(ssh_helper.get_worker_err_path(sock_path))
                self.assertTrue(err_file.exists())
                self.assertIn("stale ControlMaster", err_file.read_text(encoding="utf-8"))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2.0)
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()

    def test_close_control_master_terminates_hung_mux_and_unlinks_socket(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpcmux_") as tmpdir:
            tmp = Path(tmpdir)
            master_sock = tmp / "master-tester@gaea-c7.fairmont.rdhpcs.noaa.gov:22"
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(master_sock))
            srv.listen(1)

            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            ssh_script = bin_dir / "ssh"
            # Mock `ssh` script that refuses `-O` so close_control_master falls back to killing mux PID
            ssh_script.write_text("#!/bin/bash\nexit 255\n", encoding="utf-8")
            os.chmod(ssh_script, 0o755)

            # Symlink real python binary as `ssh` so ps shows `.../ssh` without #! rewriting
            py_ssh_bin = tmp / "ssh"
            os.symlink(sys.executable, py_ssh_bin)
            mux_proc = subprocess.Popen(
                [
                    str(py_ssh_bin),
                    "-c",
                    "import time; time.sleep(30)",
                    "-o",
                    "ControlMaster=yes",
                    "-o",
                    f"ControlPath={master_sock}",
                    "tester@gaea-c7.fairmont.rdhpcs.noaa.gov",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                time.sleep(0.15)
                found_pids = ssh_helper._find_master_mux_pids(master_sock)
                self.assertIn(mux_proc.pid, found_pids)

                # Also verify parsing of OpenSSH's `[mux]` argv[0] display format
                fake_ps = subprocess.CompletedProcess(
                    args=["ps"],
                    returncode=0,
                    stdout=f" 42424 ssh: {master_sock} [mux]\n",
                    stderr="",
                )
                with patch("ssh_helper.subprocess.run", return_value=fake_ps):
                    self.assertEqual(ssh_helper._find_master_mux_pids(master_sock), [42424])

                with patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}):
                    ssh_helper.close_control_master(
                        "tester",
                        "gaea-c7.fairmont.rdhpcs.noaa.gov",
                        sock_path=master_sock,
                    )

                self.assertFalse(master_sock.exists())
                rc = mux_proc.wait(timeout=3.0)
                self.assertNotEqual(rc, 0)
            finally:
                srv.close()
                if mux_proc.poll() is None:
                    mux_proc.kill()
                    mux_proc.wait(timeout=2.0)
                if master_sock.exists():
                    master_sock.unlink()

    def test_run_worker_server_fails_fast_when_bastion_stalls_mid_handshake(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpcmid_") as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            ssh_script = bin_dir / "ssh"
            # Emits initial bastion banner, handles ^C + hostname, then hangs silently!
            ssh_script.write_text(
                "#!/bin/bash\n"
                "echo 'NOAA RDHPCS Bastion Node'\n"
                "echo 'Hit ^C within 5 seconds to specify a hostname'\n"
                "trap 'GOT_INT=1' INT\n"
                "GOT_INT=0\n"
                "for i in 1 2 3 4 5 6 7 8 9 10; do\n"
                "  if [ \"$GOT_INT\" = \"1\" ]; then break; fi\n"
                "  sleep 0.05\n"
                "done\n"
                "trap - INT\n"
                "printf 'Enter a hostname > '\n"
                "IFS= read -r _host_choice\n"
                "echo 'Connecting to gaea71...'\n"
                "sleep 30\n",
                encoding="utf-8",
            )
            os.chmod(ssh_script, 0o755)

            sock_path = str(tmp / "midstall.sock")
            fake_master = str(tmp / "master.sock")
            Path(fake_master).touch()

            t0 = time.monotonic()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "ush" / "ssh_helper.py"),
                    "--worker-daemon",
                    "--sock-path", sock_path,
                    "--master-sock", fake_master,
                    "--worker-user", "tester",
                    "--worker-host", "gaea-c7.fairmont.rdhpcs.noaa.gov",
                    "--default-node", "gaea71",
                    "--first-byte-timeout", "0.6",
                ],
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                rc = proc.wait(timeout=6.0)
                elapsed = time.monotonic() - t0
                self.assertEqual(rc, 0)
                self.assertLess(elapsed, 3.0)
                err_file = Path(ssh_helper.get_worker_err_path(sock_path))
                self.assertTrue(err_file.exists())
                err_text = err_file.read_text(encoding="utf-8")
                self.assertIn("stale ControlMaster", err_text)
                self.assertIn("connecting to 'gaea71'", err_text)
                self.assertIn("Connecting to gaea71...", err_text)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2.0)
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()

    def test_run_worker_server_fails_immediately_on_auth_prompt(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpcauth_") as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            ssh_script = bin_dir / "ssh"
            ssh_script.write_text(
                "#!/bin/bash\n"
                "echo 'NOAA RDHPCS Bastion Node'\n"
                "echo 'Hit ^C within 5 seconds to specify a hostname'\n"
                "trap 'GOT_INT=1' INT\n"
                "GOT_INT=0\n"
                "for i in 1 2 3 4 5 6 7 8 9 10; do\n"
                "  if [ \"$GOT_INT\" = \"1\" ]; then break; fi\n"
                "  sleep 0.05\n"
                "done\n"
                "trap - INT\n"
                "printf 'Enter a hostname > '\n"
                "IFS= read -r _host_choice\n"
                "printf \"tester@gaea71's password: \"\n"
                "sleep 30\n",
                encoding="utf-8",
            )
            os.chmod(ssh_script, 0o755)

            sock_path = str(tmp / "authprompt.sock")
            fake_master = str(tmp / "master.sock")
            Path(fake_master).touch()

            t0 = time.monotonic()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "ush" / "ssh_helper.py"),
                    "--worker-daemon",
                    "--sock-path", sock_path,
                    "--master-sock", fake_master,
                    "--worker-user", "tester",
                    "--worker-host", "gaea-c7.fairmont.rdhpcs.noaa.gov",
                    "--default-node", "gaea71",
                    "--first-byte-timeout", "10.0",
                ],
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                rc = proc.wait(timeout=5.0)
                elapsed = time.monotonic() - t0
                self.assertEqual(rc, 0)
                # Detected prompt immediately without waiting full 10s idle timeout
                self.assertLess(elapsed, 3.0)
                err_file = Path(ssh_helper.get_worker_err_path(sock_path))
                self.assertTrue(err_file.exists())
                err_text = err_file.read_text(encoding="utf-8")
                self.assertIn("requested authentication", err_text)
                self.assertIn("expired ControlMaster", err_text)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2.0)
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()

    def test_run_worker_server_direct_rdhpcs_host_prompt_after_idle_wait(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpcdir_") as tmpdir:
            tmp = Path(tmpdir)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            self._create_fake_ssh_bin(bin_dir, mode="direct")

            sock_path = str(tmp / "direct_rdhpcs.sock")
            fake_master = str(tmp / "master.sock")
            Path(fake_master).touch()

            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "ush" / "ssh_helper.py"),
                    "--worker-daemon",
                    "--sock-path", sock_path,
                    "--master-sock", fake_master,
                    "--worker-user", "tester",
                    "--worker-host", "ursa-mfa.fairmont.rdhpcs.noaa.gov",
                    "--first-byte-timeout", "2.0",
                ],
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    if ssh_helper.ping_worker_session(sock_path, timeout=1.0, probe_remote=True):
                        break
                    time.sleep(0.1)
                self.assertTrue(
                    ssh_helper.ping_worker_session(sock_path, timeout=2.0, probe_remote=True),
                    f"Direct rdhpcs worker failed startup; err={Path(ssh_helper.get_worker_err_path(sock_path)).read_text() if os.path.exists(ssh_helper.get_worker_err_path(sock_path)) else ''}",
                )
            finally:
                if os.path.exists(sock_path):
                    try:
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                            s.settimeout(1.0)
                            s.connect(sock_path)
                            s.sendall(b'{"cmd": "exit"}\n')
                            s.recv(1024)
                    except OSError:
                        pass
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=3.0)
                else:
                    proc.wait(timeout=1.0)
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()

    def test_cli_cmd_auto_cleans_wedged_worker_and_stale_mux(self):
        unique_user = f"stlmux_{uuid.uuid4().hex[:8]}"
        worker_sock = ssh_helper.get_worker_sock_path(unique_user, "gaeac7")

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hpccli_") as tmpdir:
            tmp = Path(tmpdir)
            fake_home = tmp / "home"
            ssh_dir = fake_home / ".ssh"
            ssh_dir.mkdir(parents=True)
            master_sock = ssh_dir / f"master-{unique_user}@gaea-c7.fairmont.rdhpcs.noaa.gov:22"
            cfg_path = tmp / ".hpc_config"
            cfg_path.write_text(f"gaeac7: {unique_user}\n", encoding="utf-8")

            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            ssh_script = bin_dir / "ssh"
            # Mock ssh: `-O check` returns 0 (looks alive locally), `-O exit` refuses (returns 255),
            # `-tt` traps ^C, prompts hostname, prints Connecting..., then hangs mid-handshake.
            ssh_script.write_text(
                "#!/bin/bash\n"
                "if [ \"$1\" = \"-O\" ]; then\n"
                "  if [ \"$2\" = \"check\" ]; then exit 0; fi\n"
                "  exit 255\n"
                "fi\n"
                "echo 'NOAA RDHPCS Bastion Node'\n"
                "echo 'Hit ^C within 5 seconds to specify a hostname'\n"
                "trap 'GOT_INT=1' INT\n"
                "GOT_INT=0\n"
                "for i in 1 2 3 4 5 6 7 8 9 10; do\n"
                "  if [ \"$GOT_INT\" = \"1\" ]; then break; fi\n"
                "  sleep 0.05\n"
                "done\n"
                "trap - INT\n"
                "printf 'Enter a hostname > '\n"
                "IFS= read -r _host_choice\n"
                "echo 'Connecting to gaea71...'\n"
                "sleep 30\n",
                encoding="utf-8",
            )
            os.chmod(ssh_script, 0o755)

            # Bind listening master socket + sleeping mux process
            master_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            master_srv.bind(str(master_sock))
            master_srv.listen(1)

            py_ssh_bin = tmp / "ssh"
            os.symlink(sys.executable, py_ssh_bin)
            mux_proc = subprocess.Popen(
                [
                    str(py_ssh_bin),
                    "-c",
                    "import time; time.sleep(30)",
                    "-o",
                    "ControlMaster=yes",
                    "-o",
                    f"ControlPath={master_sock}",
                    f"{unique_user}@gaea-c7.fairmont.rdhpcs.noaa.gov",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Also create a pre-existing wedged worker socket + process
            wedged_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            wedged_srv.bind(worker_sock)
            wedged_srv.listen(2)
            wedged_conns = []
            stop_wedged = threading.Event()

            def _hang_worker():
                while not stop_wedged.is_set():
                    r, _, _ = select.select([wedged_srv], [], [], 0.05)
                    if wedged_srv in r:
                        c, _ = wedged_srv.accept()
                        wedged_conns.append(c)

            th = threading.Thread(target=_hang_worker, daemon=True)
            th.start()

            import contextlib
            old_home = os.environ.get("HOME")
            old_path = os.environ.get("PATH", "")
            stderr_buf = io.StringIO()
            t0 = time.monotonic()
            try:
                time.sleep(0.15)
                os.environ["HOME"] = str(fake_home)
                os.environ["PATH"] = f"{bin_dir}:{old_path}"
                with patch.object(sys, "stdin", io.StringIO("")), contextlib.redirect_stderr(stderr_buf):
                    rc = ssh_helper.main([
                        unique_user,
                        "gaeac7",
                        "--config-file", str(cfg_path),
                        "--first-byte-timeout", "0.6",
                        "--cmd", "true",
                    ])
                elapsed = time.monotonic() - t0
                self.assertEqual(rc, 1)
                self.assertLess(elapsed, 8.0)
                # Wedged worker & stale ControlMaster must both be gone now!
                self.assertFalse(os.path.exists(worker_sock))
                self.assertFalse(master_sock.exists())
                self.assertNotEqual(mux_proc.wait(timeout=3.0), 0)
                err_out = stderr_buf.getvalue()
                self.assertIn("stale ControlMaster", err_out)
                self.assertIn("Closed stale multiplex SSH session", err_out)
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                os.environ["PATH"] = old_path
                stop_wedged.set()
                th.join(timeout=1.0)
                for c in wedged_conns:
                    try:
                        c.close()
                    except OSError:
                        pass
                wedged_srv.close()
                master_srv.close()
                if mux_proc.poll() is None:
                    mux_proc.kill()
                    mux_proc.wait(timeout=2.0)
                for p in (
                    worker_sock,
                    ssh_helper.get_worker_pid_path(worker_sock),
                    ssh_helper.get_worker_err_path(worker_sock),
                    str(master_sock),
                ):
                    if os.path.exists(p):
                        os.unlink(p)


if __name__ == "__main__":
    unittest.main()
