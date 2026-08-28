import configparser
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "waydroid_config", ROOT / "scripts" / "waydroid_config.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WaydroidConfigTests(unittest.TestCase):
    def read_config(self, path):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(path)
        return parser

    def test_apply_and_restore_only_owned_values(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = base / "waydroid.cfg"
            applied = base / "applied.cfg"
            restored = base / "restored.cfg"
            state = base / "state.json"
            original.write_text(
                "[waydroid]\narch = x86_64\nmount_overlays = False\n\n"
                "[properties]\nro.build.tags = test-keys\ncustom.value = keep\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                config=original,
                output=applied,
                state=state,
                root_workaround=True,
                hires=True,
            )
            self.assertEqual(MODULE.apply_changes(args), 0)
            config = self.read_config(applied)
            self.assertEqual(config["properties"]["ro.build.tags"], "release-keys")
            self.assertEqual(config["properties"]["ro.build.selinux"], "1")
            self.assertEqual(config["waydroid"]["mount_overlays"], "True")

            config["properties"]["ro.build.tags"] = "user-choice"
            with applied.open("w", encoding="utf-8") as config_file:
                config.write(config_file)
            restore_args = SimpleNamespace(
                config=applied, output=restored, state=state
            )
            self.assertEqual(MODULE.restore_changes(restore_args), 0)
            config = self.read_config(restored)
            self.assertEqual(config["properties"]["ro.build.tags"], "user-choice")
            self.assertNotIn("ro.build.selinux", config["properties"])
            self.assertEqual(config["properties"]["custom.value"], "keep")
            self.assertEqual(config["waydroid"]["mount_overlays"], "False")

    def test_reapply_keeps_first_backup_value(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = base / "first.cfg"
            second = base / "second.cfg"
            state = base / "state.json"
            first.write_text(
                "[properties]\nro.build.tags = test-keys\n", encoding="utf-8"
            )
            args = SimpleNamespace(
                config=first,
                output=second,
                state=state,
                root_workaround=True,
                hires=False,
            )
            MODULE.apply_changes(args)
            args.config = second
            args.output = first
            MODULE.apply_changes(args)
            saved = json.loads(state.read_text(encoding="utf-8"))
            item = saved["values"]["properties.ro.build.tags"]
            self.assertEqual(item["value"], "test-keys")

    def test_restore_rejects_unexpected_state_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / "waydroid.cfg"
            state = base / "state.json"
            output = base / "output.cfg"
            config.write_text("[waydroid]\narch = x86_64\n", encoding="utf-8")
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "values": {
                            "waydroid.arch": {
                                "section": "waydroid",
                                "key": "arch",
                                "present": True,
                                "value": "x86_64",
                                "applied": "anything",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(config=config, output=output, state=state)
            with self.assertRaises(ValueError):
                MODULE.restore_changes(args)

    def test_mixed_case_option_does_not_create_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "waydroid.cfg"
            output = base / "output.cfg"
            state = base / "state.json"
            source.write_text(
                "[waydroid]\nMount_Overlays = False\n", encoding="utf-8"
            )
            args = SimpleNamespace(
                config=source,
                output=output,
                state=state,
                root_workaround=False,
                hires=True,
            )
            MODULE.apply_changes(args)
            parser = configparser.ConfigParser()
            parser.read(output)
            self.assertEqual(parser["waydroid"]["mount_overlays"], "True")
            self.assertEqual(len(parser["waydroid"]), 1)


if __name__ == "__main__":
    unittest.main()
