import json
import tempfile
import unittest
from pathlib import Path

from collector_07.workflow import (
    Scenario,
    query_rule,
    run_scenario,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures"

V1 = [{"rule": "CERT-C", "version": 1, "published_at": "2026-10-01",
       "effective_at": "2026-11-01", "format": "html", "text": "certificate required", "scope": ["child"]}]
V2 = [{"rule": "CERT-C", "version": 2, "published_at": "2026-11-10",
       "effective_at": "2026-10-20", "format": "attachment", "text": "certificate and label required", "scope": ["child"]}]
MIGRATED = [dict(V1[0], format="attachment")]


def write_fixture_tree(directory, routes, files):
    root = Path(directory) / "fixtures"
    root.mkdir()
    (root / "context.json").write_text((FIXTURES / "context.json").read_text())
    for name, payload in files.items():
        (root / name).write_text(json.dumps(payload))
    (root / "routes.json").write_text(json.dumps(routes))
    manifest = Path(directory) / "scenario.json"
    manifest.write_text(json.dumps({
        "name": "case",
        "run_id": "run-case",
        "sources": list(routes),
        "options": {"effective_on": "2026-11-05", "known_on": "2026-11-11"},
        "fixture_root": str(root),
    }))
    return manifest


def run_manifest(directory, manifest, database="state.sqlite3"):
    output = Path(directory) / "result.json"
    scenario = Scenario.load(manifest)
    result = run_scenario(scenario, Path(directory) / database)
    output.write_text(json.dumps(result, ensure_ascii=False))
    return result


class WorkflowTests(unittest.TestCase):
    def test_carrier_migration_keeps_single_version_two_carriers(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_fixture_tree(directory, {
                "/rule/v1": [{"status": 200, "file": "v1.json", "headers": {"ETag": "v1"}}],
                "/rule/migrated": [{"status": 200, "file": "migrated.json"}],
            }, {"v1.json": V1, "migrated.json": MIGRATED})
            result = run_manifest(directory, manifest)
            self.assertEqual(result["versions"], 1)
            self.assertEqual(result["carriers"], 2)
            self.assertEqual(result["selected"]["version"], 1)

    def test_304_without_cache_is_unknown_cache_not_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_fixture_tree(directory, {
                "/rule/v1": [{"status": 200, "file": "v1.json", "headers": {"ETag": "v1"}}],
                "/rule/orphan": [{"status": 304, "headers": {"ETag": "missing"}}],
            }, {"v1.json": V1})
            result = run_manifest(directory, manifest)
            by_route = {item["route"]: item["result"] for item in result["observations"]}
            self.assertEqual(by_route["/rule/orphan"], "unknown_cache")

    def test_304_with_cached_same_rule_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_fixture_tree(directory, {
                "/rule/v1": [{"status": 200, "file": "v1.json", "headers": {"ETag": "v1"}}],
                "/rule/cached": [{"status": 304, "headers": {"ETag": "v1"}}],
            }, {"v1.json": V1})
            result = run_manifest(directory, manifest)
            by_route = {item["route"]: item["result"] for item in result["observations"]}
            self.assertEqual(by_route["/rule/cached"], "unchanged")

    def test_503_is_unavailable_and_keeps_existing_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_fixture_tree(directory, {
                "/rule/v1": [{"status": 200, "file": "v1.json", "headers": {"ETag": "v1"}}],
                "/rule/down": [{"status": 503}],
            }, {"v1.json": V1})
            first = run_manifest(directory, manifest)
            second = run_manifest(directory, manifest)
            by_route = {item["route"]: item["result"] for item in first["observations"]}
            self.assertEqual(by_route["/rule/down"], "unavailable")
            self.assertEqual(first["versions"], 1)
            self.assertEqual(second["versions"], 1)
            self.assertEqual(second["selected"]["version"], 1)

    def test_retroactive_version_notifies_affected_products_once(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_fixture_tree(directory, {
                "/rule/v1": [{"status": 200, "file": "v1.json"}],
                "/rule/v2": [{"status": 200, "file": "v2.json"}],
            }, {"v1.json": V1, "v2.json": V2})
            first = run_manifest(directory, manifest)
            second = run_manifest(directory, manifest)
            self.assertEqual(first["notifications_created"], 1)
            self.assertEqual(second["notifications_created"], 0)
            self.assertEqual(len(second["notifications"]), 1)
            self.assertEqual(second["notifications"][0]["sku"], "CHILD-1")
            self.assertEqual(second["notifications"][0]["version"], 2)
            # ADULT-1 不在 child 范围内，不应被通知。
            self.assertFalse(any(item["sku"] == "ADULT-1" for item in second["notifications"]))

    def test_existing_audit_reference_is_never_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_fixture_tree(directory, {
                "/rule/v1": [{"status": 200, "file": "v1.json"}],
                "/rule/v2": [{"status": 200, "file": "v2.json"}],
            }, {"v1.json": V1, "v2.json": V2})
            result = run_manifest(directory, manifest)
            self.assertEqual(result["audits"], [{
                "id": "AUDIT-1", "sku": "CHILD-1", "on": "2026-11-05",
                "rule": "CERT-C", "rule_version": 1,
            }])
            self.assertEqual(result["selected"]["version"], 2)

    def test_known_on_excludes_unpublished_versions_even_when_retroactive(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_fixture_tree(directory, {
                "/rule/v1": [{"status": 200, "file": "v1.json"}],
                "/rule/v2": [{"status": 200, "file": "v2.json"}],
            }, {"v1.json": V1, "v2.json": V2})
            run_manifest(directory, manifest)
            database = str(Path(directory) / "state.sqlite3")
            past = query_rule(database, "CERT-C", "2026-11-05", "2026-11-06")
            now = query_rule(database, "CERT-C", "2026-11-05", "2026-11-11")
            self.assertEqual(past["version"], 1)
            self.assertEqual(past["excluded_versions"][0]["reasons"], ["published_after_known_on"])
            self.assertEqual(now["version"], 2)
            self.assertEqual(now["excluded_versions"], [])

    def test_effective_on_excludes_versions_not_yet_business_effective(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_fixture_tree(directory, {
                "/rule/v1": [{"status": 200, "file": "v1.json"}],
            }, {"v1.json": V1})
            run_manifest(directory, manifest)
            result = query_rule(str(Path(directory) / "state.sqlite3"),
                                "CERT-C", "2026-10-15", "2026-11-11")
            self.assertIsNone(result["version"])
            self.assertEqual(result["excluded_versions"][0]["reasons"], ["effective_after_effective_on"])

    def test_repeated_runs_keep_identical_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_fixture_tree(directory, {
                "/rule/v1": [{"status": 200, "file": "v1.json"}],
                "/rule/v2": [{"status": 200, "file": "v2.json"}],
            }, {"v1.json": V1, "v2.json": V2})
            first = run_manifest(directory, manifest)
            second = run_manifest(directory, manifest)
            for key in ("selected", "versions", "carriers", "notifications", "audits"):
                self.assertEqual(first[key], second[key], key)

    def test_repo_fixture_scenarios_load_and_run(self):
        for name in ("retroactive", "past_knowledge", "migration", "source_failure"):
            with tempfile.TemporaryDirectory() as directory:
                raw = json.loads((FIXTURES / f"{name}.json").read_text())
                raw["fixture_root"] = str(FIXTURES)
                manifest = Path(directory) / "scenario.json"
                manifest.write_text(json.dumps(raw))
                result = run_manifest(directory, manifest, database=f"{name}.sqlite3")
                self.assertEqual(result["run_id"], f"run-{name}")
                self.assertTrue(result["observations"])


if __name__ == "__main__":
    unittest.main()
