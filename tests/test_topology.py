import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from relay_hub import ValidationError, load_topology, validate_topology


ROOT = Path(__file__).resolve().parents[1]


def valid_topology():
    return {
        "plugins": [
            {"id": "alpha", "kind": "any kind", "namespace": "ns.alpha", "config_ref": "secret://one"},
            {"id": "beta", "kind": "other", "namespace": "ns.beta", "config_ref": "/not/opened"},
        ],
        "communities": [
            {"id": "one", "room_id": "!one:example.org", "endpoints": [
                {"plugin": "alpha", "remote_id": "remote-one"},
                {"plugin": "beta", "remote_id": "remote-one"},
            ]},
            {"id": "two", "room_id": "!two:example.org", "endpoints": [
                {"plugin": "alpha", "remote_id": "remote-two"},
            ]},
        ],
    }


class ValidatorTests(unittest.TestCase):
    def test_example_and_empty_arrays(self):
        self.assertEqual(validate_topology(valid_topology()), [])
        self.assertEqual(validate_topology({"plugins": [], "communities": []}), [])
        example = load_topology(ROOT / "examples" / "topology.toml")
        self.assertEqual(len(example["communities"]), 2)

    def test_required_keys_at_every_level(self):
        original = valid_topology()
        cases = [([], "plugins"), ([], "communities"),
                 (["plugins", 0], "id"), (["plugins", 0], "kind"),
                 (["plugins", 0], "namespace"), (["plugins", 0], "config_ref"),
                 (["communities", 0], "id"), (["communities", 0], "room_id"),
                 (["communities", 0], "endpoints"),
                 (["communities", 0, "endpoints", 0], "plugin"),
                 (["communities", 0, "endpoints", 0], "remote_id")]
        for steps, key in cases:
            with self.subTest(steps=steps, key=key):
                data = copy.deepcopy(original)
                obj = data
                for step in steps:
                    obj = obj[step]
                del obj[key]
                self.assertTrue(any(f".{key}: required" in e for e in validate_topology(data)))

    def test_unknown_keys_at_every_level(self):
        for steps in ([], ["plugins", 0], ["communities", 0], ["communities", 0, "endpoints", 0]):
            with self.subTest(steps=steps):
                data = valid_topology()
                obj = data
                for step in steps:
                    obj = obj[step]
                obj["unexpected"] = "secret-value"
                errors = validate_topology(data)
                self.assertTrue(any("unknown key" in e for e in errors))
                self.assertNotIn("secret-value", str(errors))

    def test_wrong_structures_and_strict_string_types(self):
        self.assertIn("expected a table", validate_topology([])[0])
        for field in ("plugins", "communities"):
            for bad in ("array", {}, 0, True, None):
                with self.subTest(field=field, bad=bad):
                    data = valid_topology()
                    data[field] = bad
                    self.assertIn("expected an array", str(validate_topology(data)))
        for field in ("plugins", "communities"):
            data = valid_topology()
            data[field] = [False]
            self.assertIn("expected a table", str(validate_topology(data)))
        data = valid_topology()
        data["communities"][0]["endpoints"] = [{} , 42]
        self.assertIn("expected a table", str(validate_topology(data)))
        data["communities"][0]["endpoints"] = {}
        self.assertIn("expected an array", str(validate_topology(data)))
        for steps, field in [(["plugins", 0], "id"), (["plugins", 0], "kind"),
                             (["plugins", 0], "namespace"), (["plugins", 0], "config_ref"),
                             (["communities", 0], "id"), (["communities", 0], "room_id"),
                             (["communities", 0, "endpoints", 0], "plugin"),
                             (["communities", 0, "endpoints", 0], "remote_id")]:
            for bad in ("", "  ", True, 0, [], {}, None):
                with self.subTest(steps=steps, field=field, bad=bad):
                    data = valid_topology()
                    obj = data
                    for step in steps:
                        obj = obj[step]
                    obj[field] = bad
                    self.assertTrue(any(f".{field}: expected a nonblank string" in e
                                        for e in validate_topology(data)))

    def test_duplicates_and_isolation(self):
        cases = [("plugins", "id"), ("plugins", "namespace"),
                 ("communities", "id"), ("communities", "room_id")]
        for collection, field in cases:
            with self.subTest(collection=collection, field=field):
                data = valid_topology()
                data[collection][1][field] = data[collection][0][field]
                self.assertTrue(any(f"{collection}[1].{field}: duplicates {collection}[0].{field}" in e
                                    for e in validate_topology(data)))
        data = valid_topology()
        data["communities"][1]["endpoints"][0]["remote_id"] = "remote-one"
        errors = validate_topology(data)
        self.assertTrue(any("communities[1].endpoints[0].remote_id: duplicates communities[0].endpoints[0].remote_id" in e for e in errors))
        data = valid_topology()
        data["communities"][0]["endpoints"].append({"plugin": "alpha", "remote_id": "different"})
        self.assertTrue(any("communities[0].endpoints[2].plugin: duplicates" in e for e in validate_topology(data)))
        data = valid_topology()
        data["communities"][0]["endpoints"][0]["plugin"] = "missing"
        self.assertTrue(any("unknown plugin id" in e for e in validate_topology(data)))

    def test_room_id_shape(self):
        for bad in ("#alias:example.org", "local:example.org", "!room:", "!", "!room:bad host", "!a\0b:example.org"):
            with self.subTest(bad=bad):
                data = valid_topology()
                data["communities"][0]["room_id"] = bad
                self.assertIn("expected a Matrix room ID", str(validate_topology(data)))
        for room_id in ("!room:[::1]:8448", "!Nhcu5BS-UMnFX7hBVfVSoXiD7OgH6iRT-xyIuqDnpYQ",
                        "!a b:example.org", "!a\nb:example.org", "!:example.org"):
            with self.subTest(room_id=room_id):
                data = valid_topology()
                data["communities"][0]["room_id"] = room_id
                self.assertEqual(validate_topology(data), [])

    def test_config_ref_is_opaque_and_errors_never_contain_values(self):
        data = valid_topology()
        data["plugins"][0]["config_ref"] = "TOP_SECRET"
        data["plugins"][0]["unexpected"] = "TOP_SECRET"
        data["communities"][0]["endpoints"][0]["plugin"] = "TOP_SECRET"
        self.assertNotIn("TOP_SECRET", str(validate_topology(data)))
        data["plugins"][0].pop("unexpected")
        data["communities"][0]["endpoints"][0]["plugin"] = "alpha"
        self.assertEqual(validate_topology(data), [])


class FileAndCLITests(unittest.TestCase):
    def cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-m", "relay_hub", *map(str, args)],
                              cwd=ROOT, env=env, text=True, capture_output=True, check=False)

    def test_cli_success(self):
        result = self.cli("check", ROOT / "examples" / "topology.toml")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("static inventory", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_cli_errors_and_no_secret_echo(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "topology.toml"
            path.write_text('plugins = []\ncommunities = []\nsecret = "TOP_SECRET"\n', encoding="utf-8")
            first = self.cli("check", path)
            second = self.cli("check", path)
            self.assertNotEqual(first.returncode, 0)
            self.assertEqual(first.stdout, "")
            self.assertEqual(first.stderr, second.stderr)
            self.assertIn("unknown key", first.stderr)
            self.assertNotIn("TOP_SECRET", first.stdout + first.stderr)
            path.write_text('secret = "TOP_SECRET', encoding="utf-8")
            result = self.cli("check", path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid TOML", result.stderr)
            self.assertNotIn("TOP_SECRET", result.stdout + result.stderr)
            path.write_bytes(b"\xffTOP_SECRET")
            result = self.cli("check", path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("UTF-8", result.stderr)
            result = self.cli("check", Path(temp) / "missing")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot read", result.stderr)

    def test_toml_parser_errors_and_file_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "file"
            for contents in ('plugins = []\nplugins = []', 'plugins = [1,\n', '[plugins\n'):
                with self.subTest(contents=contents):
                    path.write_text(contents, encoding="utf-8")
                    with self.assertRaises(ValidationError) as caught:
                        load_topology(path)
                    self.assertIn("invalid TOML", str(caught.exception))
            for contents in ('x = ' + '[' * 2000 + ']' * 2000, 'x = ' + '1' * 5000):
                path.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValidationError) as caught:
                    load_topology(path)
                self.assertIn("parser limits", str(caught.exception))
                result = self.cli("check", path)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Traceback", result.stderr)
            with self.assertRaises(ValidationError):
                load_topology(Path(temp))
            with self.assertRaises(ValidationError) as caught:
                load_topology("invalid\0SECRET")
            self.assertNotIn("SECRET", str(caught.exception))

    def test_cli_usage(self):
        for args in ((), ("check",), ("unknown",)):
            with self.subTest(args=args):
                self.assertNotEqual(self.cli(*args).returncode, 0)


if __name__ == "__main__":
    unittest.main()
