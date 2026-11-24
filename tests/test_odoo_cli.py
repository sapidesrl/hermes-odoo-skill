"""Offline tests: run with `python3 -m unittest discover -s tests`."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "skills" / "odoo" / "scripts"))

import fake_odoo  # noqa: E402
import odoo_cli  # noqa: E402


class CliTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.server, url = fake_odoo.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "ODOO_URL": url, "ODOO_DB": "testdb", "ODOO_USERNAME": "admin", "ODOO_PASSWORD": "secret",
            "ODOO_PROTOCOL": "xmlrpc", "ODOO_HERMES_HOME": self.tmp.name, "HERMES_HOME": self.tmp.name,
        }
        self._old = {k: os.environ.get(k) for k in list(self.env) + ["ODOO_HERMES_ALLOW_WRITES", "ODOO_API_KEY",
                                                                     "ODOO_HERMES_ALLOWED_METHODS", "ODOO_HERMES_MAX_WRITE"]}
        for k in self._old:
            os.environ.pop(k, None)
        os.environ.update(self.env)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()
        for k, v in self._old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = odoo_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def writes_on(self):
        os.environ["ODOO_HERMES_ALLOW_WRITES"] = "1"

    def methods_called(self):
        return [c[1] for c in self.fake.calls]

    # ---------------------------------------------------------------- reads
    def test_doctor(self):
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("17.0", out)
        self.assertIn("read-only", out)

    def test_find_dsl(self):
        code, out, _ = self.run_cli("--json", "find", "res.partner", "name ~ acme, is_company = true")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([r["id"] for r in data["records"]], [1])
        self.assertEqual(data["domain"], [["name", "ilike", "acme"], ["is_company", "=", True]])

    def test_parse_where(self):
        self.assertEqual(odoo_cli.parse_where("!phone, email, id in [1,2]"),
                         [["phone", "=", False], ["email", "!=", False], ["id", "in", [1, 2]]])
        self.assertEqual(odoo_cli.parse_where('name ~ "Acme, Inc"'), [["name", "ilike", "Acme, Inc"]])
        self.assertRegex(odoo_cli.parse_where("date >= @month_start")[0][2], r"^\d{4}-\d{2}-01$")

    def test_archived_hidden_by_default(self):
        _, out, _ = self.run_cli("--json", "find", "res.partner")
        self.assertNotIn(4, [r["id"] for r in json.loads(out)["records"]])
        _, out, _ = self.run_cli("--json", "find", "res.partner", "--archived")
        self.assertIn(4, [r["id"] for r in json.loads(out)["records"]])

    def test_hygiene_is_read_only(self):
        code, out, _ = self.run_cli("hygiene")
        self.assertEqual(code, 0)
        self.assertIn("info@acme.test", out)
        self.assertFalse({"write", "create", "unlink"} & set(self.methods_called()))

    def test_360(self):
        code, out, _ = self.run_cli("360", "Acme Corp")
        self.assertEqual(code, 0, out)
        self.assertIn("Acme Corp", out)

    # --------------------------------------------------------------- writes
    def test_update_dry_run_by_default(self):
        self.writes_on()
        code, out, _ = self.run_cli("update", "res.partner", "--ids", "1", "--set", "phone=999")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("111", out)
        self.assertNotIn("write", self.methods_called())
        self.assertEqual(self.fake.data["res.partner"][1]["phone"], "111")

    def test_read_only_mode_blocks_apply(self):
        code, _, err = self.run_cli("update", "res.partner", "--ids", "1", "--set", "phone=999", "--apply")
        self.assertEqual(code, 3)
        self.assertIn("Read-only", err)
        self.assertNotIn("write", self.methods_called())

    def test_apply_journal_and_undo(self):
        self.writes_on()
        code, out, _ = self.run_cli("--json", "update", "res.partner", "--ids", "1,3", "--set", "phone=0612", "--apply")
        self.assertEqual(code, 0, out)
        entry = json.loads(out)["journal"]
        self.assertEqual(self.fake.data["res.partner"][1]["phone"], "0612")
        # undo: dry-run first
        code, out, _ = self.run_cli("undo", entry)
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.fake.data["res.partner"][1]["phone"], "0612")
        code, out, err = self.run_cli("undo", entry, "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.data["res.partner"][1]["phone"], "111")
        self.assertEqual(self.fake.data["res.partner"][3]["phone"], "333")
        # second undo refused
        code, _, err = self.run_cli("undo", entry, "--apply")
        self.assertEqual(code, 3)
        self.assertIn("already undone", err)

    def test_undo_detects_conflicts(self):
        self.writes_on()
        _, out, _ = self.run_cli("--json", "update", "res.partner", "--ids", "1", "--set", "phone=222", "--apply")
        entry = json.loads(out)["journal"]
        self.fake.data["res.partner"][1]["phone"] = "changed-by-human"
        code, _, err = self.run_cli("undo", entry, "--apply")
        self.assertEqual(code, 3)
        self.assertIn("changed", err)
        self.assertEqual(self.fake.data["res.partner"][1]["phone"], "changed-by-human")

    def test_many2one_by_name_and_m2m_add_only(self):
        self.writes_on()
        code, out, err = self.run_cli("update", "res.partner", "--ids", "3", "--set", "parent_id=@Acme Corp",
                                      "--values", '{"category_id": ["@Prospect"]}', "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.data["res.partner"][3]["parent_id"], 1)
        self.assertEqual(self.fake.data["res.partner"][3]["category_id"], [2])

    def test_destructive_x2many_commands_refused(self):
        self.writes_on()
        for cmd in ([[2, 1, 0]], [[5, 0, 0]]):
            code, _, err = self.run_cli("update", "res.partner", "--ids", "1", "--values",
                                        json.dumps({"category_id": cmd}), "--apply")
            self.assertEqual(code, 3, err)
        code, _, err = self.run_cli("update", "res.partner", "--ids", "1", "--values",
                                    json.dumps({"child_ids": [[3, 2, 0]]}), "--apply")
        self.assertEqual(code, 3)
        self.assertIn("one2many", err)

    def test_protected_fields_and_models(self):
        self.writes_on()
        for field in ("active", "state", "password"):
            code, _, err = self.run_cli("update", "res.partner", "--ids", "1", "--set", "%s=x" % field, "--apply")
            self.assertEqual(code, 3, field)
        code, _, err = self.run_cli("update", "res.users", "--ids", "2", "--set", "name=x", "--apply")
        self.assertEqual(code, 3)
        self.assertIn("protected", err)
        self.assertNotIn("write", self.methods_called())

    def test_mass_edit_cap_and_empty_where(self):
        self.writes_on()
        os.environ["ODOO_HERMES_MAX_WRITE"] = "1"
        code, _, err = self.run_cli("update", "res.partner", "--where", "name ~ acme", "--set", "phone=1", "--apply")
        self.assertEqual(code, 3)
        self.assertIn("more than 1", err)
        code, _, err = self.run_cli("update", "res.partner", "--where", "*", "--set", "phone=1", "--apply")
        self.assertEqual(code, 3)
        self.assertIn("empty", err)

    def test_archive_unarchive_cycle(self):
        self.writes_on()
        _, out, _ = self.run_cli("--json", "archive", "res.partner", "--ids", "2", "--apply")
        entry = json.loads(out)["journal"]
        self.assertFalse(self.fake.data["res.partner"][2]["active"])
        code, _, err = self.run_cli("undo", entry, "--apply")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.fake.data["res.partner"][2]["active"])

    def test_create_then_undo_archives(self):
        self.writes_on()
        code, out, err = self.run_cli("--json", "create", "res.partner", "--set", "name=New Co", "--set", "zip=75001", "--apply")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        new_id = data["ids"][0]
        self.assertEqual(self.fake.data["res.partner"][new_id]["zip"], "75001")
        self.run_cli("undo", data["journal"], "--apply")
        self.assertFalse(self.fake.data["res.partner"][new_id]["active"])
        self.assertIn(new_id, self.fake.data["res.partner"])  # archived, never deleted

    def test_note(self):
        self.writes_on()
        code, out, err = self.run_cli("note", "res.partner", "1", "Called, will order next week", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.fake.data["mail.message"]), 1)

    def test_call_rules(self):
        self.writes_on()
        code, _, err = self.run_cli("call", "res.partner", "unlink", "--ids", "1", "--apply")
        self.assertEqual(code, 3)
        self.assertIn("blocked", err)
        code, _, err = self.run_cli("call", "res.partner", "action_bump", "--ids", "1", "--apply")
        self.assertEqual(code, 3)
        self.assertIn("allow-listed", err)
        os.environ["ODOO_HERMES_ALLOWED_METHODS"] = "action_bump,unlink"
        code, _, err = self.run_cli("call", "res.partner", "unlink", "--ids", "1", "--apply")
        self.assertEqual(code, 3)  # hard block wins over allow-list
        code, out, err = self.run_cli("call", "res.partner", "action_bump", "--ids", "1", "--apply")
        self.assertEqual(code, 0, err)
        code, out, _ = self.run_cli("call", "res.partner", "search_count", "--kwargs", '{"domain": []}')
        self.assertEqual(code, 0)
        self.assertNotIn("unlink", self.methods_called())

    def test_export_never_overwrites(self):
        target = Path(self.tmp.name) / "out.csv"
        code, _, _ = self.run_cli("export", "res.partner", "--out", str(target))
        self.assertEqual(code, 0)
        self.assertIn("Acme Corp", target.read_text())
        code, _, err = self.run_cli("export", "res.partner", "--out", str(target))
        self.assertEqual(code, 3)


class Json2Test(CliTest):
    """Same safety guarantees over the JSON-2 API (Odoo 19+, auto-detected)."""

    def setUp(self):
        super().setUp()
        self.server.shutdown()
        self.server.server_close()
        self.fake, self.server, url = fake_odoo.start_json2()
        os.environ.update({"ODOO_URL": url, "ODOO_PROTOCOL": "auto", "ODOO_API_KEY": "k3y"})
        os.environ.pop("ODOO_PASSWORD", None)

    def test_doctor(self):
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 0, err)
        self.assertIn("json2", out)

    def test_bad_key(self):
        os.environ["ODOO_API_KEY"] = "nope"
        code, _, err = self.run_cli("find", "res.partner")
        self.assertEqual(code, 4)


if __name__ == "__main__":
    unittest.main()
