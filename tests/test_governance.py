"""Role schemas: no bronze PHI string reaches any non-PHI role; roles can only
read their own schema. Fake model providers only."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import bronze_fixture as B  # noqa: E402

from governance import roles  # noqa: E402
from llm import providers as P  # noqa: E402
from llm.phi import find_phi  # noqa: E402
from waves import pipeline  # noqa: E402
from waves.framework import Ctx  # noqa: E402


class TestGovernance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"IPA_ROUTING_LOG": os.path.join(cls.td.name, "r.jsonl")})
        cls.env.start()
        cls.prov = mock.patch.dict(P.PROVIDERS, {"local": B.FakeNER()})
        cls.prov.start()
        cls.con = B.build()
        pipeline.run(cls.con, Ctx(providers={"wave1.billing.redact_names": "local"}))

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.prov.stop()
        cls.env.stop()
        cls.td.cleanup()

    def values(self, role):
        """{view: [every cell as text]} for a role schema."""
        out = {}
        for v in roles.tables(self.con, role):
            rows = self.con.execute(f"SELECT * FROM role_{role}.{v}").fetchall()
            out[v] = [str(c) for r in rows for c in r if c is not None]
        return out

    def test_every_role_schema_built(self):
        for role in roles.ROLES:
            self.assertTrue(roles.tables(self.con, role), role)

    def test_no_bronze_phi_in_non_phi_roles(self):
        phi = B.phi_values()
        for role in roles.ROLES:
            if role in roles.PHI_ROLES:
                continue
            for view, cells in self.values(role).items():
                blob = "\n".join(cells)
                for v in phi:
                    self.assertNotIn(v, blob, f"{v!r} leaked into role_{role}.{view}")

    def test_phi_role_does_see_raw_text(self):
        blob = "\n".join(self.values("billing_phi")["epcrs"])
        self.assertIn("901-00-0001", blob)

    def test_raw_text_column_only_in_phi_role(self):
        schemas = {r[0] for r in self.con.execute(
            "SELECT DISTINCT table_schema FROM information_schema.columns "
            "WHERE column_name = 'raw_text' AND table_schema LIKE 'role_%'").fetchall()}
        self.assertEqual(schemas, {"role_billing_phi"})

    def test_exec_has_no_identifier_patterns(self):
        for view, cells in self.values("exec").items():
            for c in cells:
                self.assertEqual(find_phi(c), [], (view, c))

    def test_query_scoped_to_own_schema(self):
        rows = roles.query(self.con, "billing", "SELECT count(*) AS n FROM epcr_deid")
        self.assertEqual(rows, [{"n": 6}])
        self.assertEqual(roles.query(self.con, "exec", "SELECT count(*) AS n FROM role_exec.aog_events"), [{"n": 1}])
        for role, sql in [("billing", "SELECT raw_text FROM bronze.epcrs"),
                          ("billing", "SELECT raw_text FROM role_billing_phi.epcrs"),
                          ("exec", "SELECT * FROM silver.epcr_deid"),
                          ("exec", "DROP VIEW role_exec.aog_events"),
                          ("ops", "SELECT 1; SELECT 2"),
                          ("ops", "SELECT * FROM read_csv('x')")]:
            with self.assertRaises(roles.AccessDenied, msg=sql):
                roles.query(self.con, role, sql)
        with self.assertRaises(Exception):  # unqualified name does not resolve outside the role schema
            roles.query(self.con, "billing", "SELECT raw_text FROM epcrs")
        with self.assertRaises(roles.AccessDenied):
            roles.query(self.con, "nobody", "SELECT 1")


if __name__ == "__main__":
    unittest.main()
