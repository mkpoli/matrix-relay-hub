"""Offline checks for the optional QQ bridge overlay."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
FILES = ["-f", "deploy/compose.yaml", "-f", "deploy/compose.qq.yaml"]
DOCKER = shutil.which("docker") or "docker"
NETWORK = "matrix-relay-hub-offline-test"
DATA_DIR = "/tmp/matrix-relay-hub-offline-test/qq"
IMAGE = "matrix-relay-hub/qq-bridge:offline-test"


def compose_env():
    return {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "DOCKER_CONFIG": "/nonexistent",
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "MATRIX_NETWORK": NETWORK,
        "QQ_DATA_DIR": DATA_DIR,
        "QQ_UID": "1000",
        "QQ_GID": "1000",
    }


def compose(*args, env=None):
    return subprocess.run(
        [DOCKER, "compose", *FILES, *args],
        cwd=ROOT,
        env=env or compose_env(),
        text=True,
        capture_output=True,
        check=False,
    )


class QQOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            result = subprocess.run(
                [DOCKER, "compose", "version"],
                cwd=ROOT,
                env=compose_env(),
                text=True,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            raise unittest.SkipTest("docker compose is unavailable") from None
        if result.returncode:
            raise unittest.SkipTest("docker compose is unavailable")

    def config(self, profiles, env=None):
        args = [arg for profile in profiles for arg in ("--profile", profile)]
        result = compose(*args, "config", "--format", "json", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_qq_profile_only(self):
        config = self.config(["qq"])
        self.assertEqual(set(config["services"]), {"qq"})
        service = config["services"]["qq"]
        self.assertEqual(service["image"], "matrix-relay-hub/qq-bridge:local")
        self.assertEqual(Path(service["build"]["context"]).resolve(), ROOT)
        self.assertEqual(service["build"]["dockerfile"], "deploy/qq/Dockerfile")
        self.assertEqual(service["user"], "1000:1000")
        self.assertEqual(set(service["networks"]), {"matrix"})
        self.assertEqual(service["networks"]["matrix"]["aliases"], ["matrix-relay-qq"])
        self.assertNotIn("restart", service)
        self.assertNotIn("ports", service)
        self.assertNotIn("container_name", service)
        self.assertNotIn("environment", service)
        self.assertEqual(len(service["volumes"]), 1)
        volume = service["volumes"][0]
        self.assertEqual(volume["type"], "bind")
        self.assertEqual(volume["source"], DATA_DIR)
        self.assertEqual(volume["target"], "/data")
        self.assertIs(volume["bind"]["create_host_path"], False)
        self.assertEqual(config["networks"]["matrix"]["name"], NETWORK)
        self.assertIs(config["networks"]["matrix"]["external"], True)

    def test_custom_image_name(self):
        env = dict(compose_env(), QQ_BRIDGE_IMAGE=IMAGE)
        config = self.config(["qq"], env=env)
        self.assertEqual(config["services"]["qq"]["image"], IMAGE)

    def test_qq_with_base_profiles(self):
        config = self.config(["discord", "qq"])
        self.assertEqual(set(config["services"]), {"discord", "qq"})

    def test_data_owner_is_required(self):
        env = compose_env()
        del env["QQ_UID"]
        result = compose("--profile", "qq", "config", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("QQ_UID", result.stderr)


if __name__ == "__main__":
    unittest.main()
