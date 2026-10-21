"""CLI routing only: stubs are not end-to-end execution evidence."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from types import ModuleType
from unittest.mock import patch

from support import FixtureCase
from honeytem.cli import main, _EXTENSIONS


class FeatureCLIRoutingTests(FixtureCase):
    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_extension_commands_route_only_to_selected_handler(self):
        for module_name, commands in _EXTENSIONS.items():
            with self.subTest(module=module_name):
                fake = ModuleType("honeytem." + module_name)
                fake.register_cli = lambda parsers, names=commands: [parsers.add_parser(name) for name in names]
                fake.handle_cli = lambda args: {"selected": args.command, "publish_ready": False}
                with patch.dict("sys.modules", {"honeytem." + module_name: fake}):
                    rc, out, err = self.invoke([commands[0]])
                self.assertEqual(rc, 0, err)
                self.assertEqual(json.loads(out)["selected"], commands[0])

    def test_existing_validation_does_not_import_extension_modules(self):
        with patch("importlib.import_module", side_effect=AssertionError("unexpected optional import")):
            rc, out, err = self.invoke(["validate", str(self.manifest_path)])
        self.assertEqual(rc, 0, err)
        self.assertTrue(json.loads(out)["valid"])

    def test_top_level_help_names_extensions_without_loading_them(self):
        with patch("importlib.import_module", side_effect=AssertionError("unexpected optional import")):
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
                main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        for commands in _EXTENSIONS.values():
            for command in commands:
                self.assertIn(command, output.getvalue())
