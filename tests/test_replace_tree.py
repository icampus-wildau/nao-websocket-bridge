"""The staged tree replaces one canonical directory and keeps the token file."""

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


class ReplaceTreeTests(unittest.TestCase):
    def test_swap_keeps_the_token_and_removes_the_previous_tree(self) -> None:
        script = Path(__file__).resolve().parents[1] / "packaging" / "replace_tree.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canon = root / "nao-audio-bridge"
            stage = root / "nao-audio-bridge.stage"
            canon.mkdir()
            (canon / "bridge.env").write_text("NAO_BRIDGE_TOKEN=kept\n", encoding="utf-8")
            (canon / "audio_bridge.py").write_text("old\n", encoding="utf-8")
            stage.mkdir()
            (stage / "audio_bridge.py").write_text("new\n", encoding="utf-8")
            launch = stage / "run_forever.sh"
            launch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launch.chmod(launch.stat().st_mode | stat.S_IEXEC)
            env = os.environ.copy()
            env.update(
                NAO_CANON=str(canon),
                NAO_STAGE=str(stage),
                NAO_MARKER="audio_bridge.py",
                NAO_HOME_TOKEN="nao-audio-bridge",
                NAO_KEEP="bridge.env",
                NAO_SKIP_STOP="1",
            )
            completed = subprocess.run(["sh", str(script)], env=env, text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((canon / "audio_bridge.py").read_text(encoding="utf-8"), "new\n")
            self.assertEqual((canon / "bridge.env").read_text(encoding="utf-8"), "NAO_BRIDGE_TOKEN=kept\n")
            self.assertFalse((root / "nao-audio-bridge.prev").exists())
            self.assertFalse(stage.exists())


if __name__ == "__main__":
    unittest.main()
