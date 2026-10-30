"""Exercise the laptop installer with a first-contact SSH prompt, without a NAO."""

import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tarfile
import tempfile
import termios
import time
import unittest


class InstallerTests(unittest.TestCase):
    def test_first_ssh_host_key_is_confirmed_and_password_is_used(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            bridge = work / "nao-websocket-bridge-main"
            protocol = work / "nao-websocket-bridge-protocol-main"
            bridge.mkdir()
            protocol.mkdir()
            for name in ("audio_bridge.py", "SimpleWebSocketServer", "packaging"):
                source = root / name
                if source.is_dir():
                    shutil.copytree(source, bridge / name)
                else:
                    shutil.copy2(source, bridge / name)
            (protocol / "nao_websocket_bridge_protocol").mkdir()
            (protocol / "nao_websocket_bridge_protocol" / "__init__.py").write_text("")
            for name, source in (("bridge", bridge), ("protocol", protocol)):
                with tarfile.open(work / (name + ".tar.gz"), "w:gz") as archive:
                    archive.add(source, arcname=source.name)
            fake_bin = work / "bin"
            fake_bin.mkdir()
            scripts = {
                "curl": '''#!/bin/sh
case "$2" in *nao-websocket-bridge-protocol*) file=protocol;; *) file=bridge;; esac
cp "$TEST_DIR/$file.tar.gz" "$4"
''',
                "ssh": '''#!/bin/sh
case " $* " in *" -O exit "*) exit 0;; esac
if [ ! -f "$TEST_DIR/confirmed" ]; then
  answer=$(SSH_ASKPASS_PROMPT=confirm "$SSH_ASKPASS" 'The authenticity of host. Continue connecting (yes/no)?')
  [ "$answer" = yes ] || exit 1
  answer=$(SSH_ASKPASS_PROMPT= "$SSH_ASKPASS" 'Password:')
  [ "$answer" = "$NAO_PASSWORD" ] || exit 1
  touch "$TEST_DIR/confirmed"
fi
''',
                "scp": "#!/bin/sh\nexit 0\n",
            }
            for name, content in scripts.items():
                path = fake_bin / name
                path.write_text(content)
                path.chmod(0o755)
            master, slave = pty.openpty()
            def controlling_terminal():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            env = os.environ.copy()
            env.update(PATH=str(fake_bin) + os.pathsep + env["PATH"], TEST_DIR=tmp,
                       NAO_IP="192.0.2.42", NAO_PASSWORD="test-password")
            process = subprocess.Popen(["sh", str(root / "packaging/install.sh")],
                stdin=slave, stdout=slave, stderr=slave, env=env,
                preexec_fn=controlling_terminal)
            os.close(slave)
            output = b""
            try:
                deadline = time.monotonic() + 15
                replied = False
                while process.poll() is None and time.monotonic() < deadline:
                    readable, _, _ = select.select([master], [], [], 0.2)
                    if readable:
                        try:
                            output += os.read(master, 4096)
                        except OSError:
                            break
                        if b"yes/no" in output and not replied:
                            os.write(master, b"yes\n")
                            replied = True
                if process.poll() is None:
                    process.kill()
                process.wait()
                self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
                self.assertTrue(replied, output.decode(errors="replace"))
                self.assertTrue((work / "confirmed").is_file())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)


if __name__ == "__main__":
    unittest.main()
