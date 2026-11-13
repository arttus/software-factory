import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCHEMAS = REPO / "factory" / "schemas"


class SchemaFileTests(unittest.TestCase):
    def test_every_schema_is_valid_json(self):
        schema_files = sorted(SCHEMAS.glob("*.schema.json"))
        self.assertEqual(len(schema_files), 4)
        for path in schema_files:
            with self.subTest(schema=path.name):
                data = json.loads(path.read_text())
                self.assertEqual(data["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertIn("required", data)

    def test_contract_schemas_name_their_version(self):
        config = json.loads((SCHEMAS / "config.schema.json").read_text())
        self.assertEqual(config["properties"]["schema_version"]["const"], 2)
        request = json.loads((SCHEMAS / "invocation.schema.json").read_text())
        self.assertEqual(request["properties"]["schema"]["const"],
                         "factory.invocation.request/1")
        result = json.loads((SCHEMAS / "result.schema.json").read_text())
        self.assertEqual(result["properties"]["schema"]["const"],
                         "factory.invocation.result/1")
        event = json.loads((SCHEMAS / "event.schema.json").read_text())
        self.assertEqual(event["properties"]["schema"]["const"],
                         "factory.invocation.event/1")


if __name__ == "__main__":
    unittest.main()
