"""Offline checks for the bridge Compose configuration."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = "deploy/compose.yaml"
DOCKER = shutil.which("docker") or "docker"
NETWORK = "matrix-relay-hub-offline-test"
DATA_DIRS = {
    "discord": "/tmp/matrix-relay-hub-offline-test/discord",
    "telegram": "/tmp/matrix-relay-hub-offline-test/telegram",
    "irc": "/tmp/matrix-relay-hub-offline-test/irc",
}
IMAGES = {
    "discord": "dock.mau.dev/mautrix/discord",
    "telegram": "dock.mau.dev/mautrix/telegram",
    "irc": "matrixdotorg/matrix-appservice-irc",
}
FAKE_SECRETS = {
    "AS_TOKEN": "offline-test-as-token-do-not-render",
    "HS_TOKEN": "offline-test-hs-token-do-not-render",
    "DISCORD_TOKEN": "offline-test-discord-token-do-not-render",
    "TELEGRAM_TOKEN": "offline-test-telegram-token-do-not-render",
}


def compose_env(include_network=True, aliases=None):
    # Do not inherit Compose, Docker, bridge, proxy, or project settings.
    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "DOCKER_CONFIG": "/nonexistent",
        "COMPOSE_DISABLE_ENV_FILE": "1",
        **FAKE_SECRETS,
        **{f"{service.upper()}_DATA_DIR": path for service, path in DATA_DIRS.items()},
    }
    if include_network:
        env["MATRIX_NETWORK"] = NETWORK
    if aliases:
        env.update({f"{service.upper()}_NETWORK_ALIAS": alias for service, alias in aliases.items()})
    return env


def compose(*args, include_network=True, aliases=None):
    return subprocess.run(
        [DOCKER, "compose", "-f", COMPOSE_FILE, *args],
        cwd=ROOT,
        env=compose_env(include_network, aliases),
        text=True,
        capture_output=True,
        check=False,
    )


class ComposeConfigTests(unittest.TestCase):
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

    def assert_no_secrets(self, output):
        for secret in FAKE_SECRETS.values():
            self.assertNotIn(secret, output)

    def assert_config(self, profiles, aliases=None):
        profile_args = [arg for profile in profiles for arg in ("--profile", profile)]
        result = compose(*profile_args, "config", "--format", "json", aliases=aliases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_secrets(result.stdout + result.stderr)
        config = json.loads(result.stdout)
        self.assertNotIn("secrets", config)
        self.assertEqual(set(config["services"]), set(profiles))
        self.assertEqual(config["networks"]["matrix"]["name"], NETWORK)
        self.assertIs(config["networks"]["matrix"]["external"], True)

        for profile in profiles:
            with self.subTest(service=profile):
                service = config["services"][profile]
                self.assertRegex(
                    service["image"],
                    rf"^{IMAGES[profile].replace('.', r'[.]')}@sha256:[0-9a-f]{{64}}$",
                )
                self.assertEqual(set(service["networks"]), {"matrix"})
                expected_alias = aliases[profile] if aliases else f"matrix-relay-{profile}"
                self.assertEqual(service["networks"]["matrix"]["aliases"], [expected_alias])
                self.assertNotIn("restart", service)
                self.assertNotIn("ports", service)
                self.assertNotIn("container_name", service)
                self.assertNotIn("secrets", service)
                self.assertNotIn("environment", service)
                self.assertEqual(len(service["volumes"]), 1)
                volume = service["volumes"][0]
                self.assertEqual(volume["type"], "bind")
                self.assertEqual(volume["source"], DATA_DIRS[profile])
                self.assertEqual(volume["target"], "/data")
                self.assertIs(volume["bind"]["create_host_path"], False)

    def test_each_profile(self):
        for profile in DATA_DIRS:
            with self.subTest(profile=profile):
                self.assert_config([profile])

    def test_combined_profiles(self):
        self.assert_config(list(DATA_DIRS))

    def test_custom_network_aliases(self):
        aliases = {profile: f"offline-custom-{profile}" for profile in DATA_DIRS}
        self.assertEqual(len(set(aliases.values())), len(DATA_DIRS))
        self.assert_config(list(DATA_DIRS), aliases=aliases)

    def test_network_is_required(self):
        result = compose("--profile", "discord", "config", "--format", "json", include_network=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MATRIX_NETWORK", result.stderr)
        self.assert_no_secrets(result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
