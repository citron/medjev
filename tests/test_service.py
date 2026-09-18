import sqlite3
import unittest

from medjev import AuditLog, DecisionService, FeatureNormalizer, ModelMonitor, ReadOnlyWarehouse, ThresholdPolicy, validate_predictions
from medjev.service import AccessDenied


class ServiceTests(unittest.TestCase):
    def test_normalizes_and_returns_human_review_fhir_response(self):
        normalizer = FeatureNormalizer({("http://loinc.org", "8867-4"): "heart_rate"}, "features-v1")
        features = normalizer.normalize_observations([{"id": "obs-1", "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]}, "valueQuantity": {"value": 125}}])
        service = DecisionService(ThresholdPolicy("deterioration", "model-v1", {"heart_rate": 0.02}, -3, 0.25), AuditLog(b"x" * 32))
        result = service.assess("patient-1", features, "clinician-1")

        self.assertEqual(result["resourceType"], "GuidanceResponse")
        self.assertEqual(result["outputParameters"]["parameter"][2]["valueBoolean"], True)
        service.review(result["id"], "patient-1", "clinician-1", False, "Patient is improving clinically.")
        self.assertEqual(service.audit_log.events[-1]["action"], "reviewed")
        self.assertNotIn("patient-1", str(service.audit_log.events))

    def test_warehouse_rejects_mutations(self):
        def connection():
            database = sqlite3.connect(":memory:")
            database.execute("create table patients (id integer)")
            database.execute("insert into patients values (1)")
            return database

        warehouse = ReadOnlyWarehouse(connection)
        self.assertEqual(warehouse.query("select id from patients"), [{"id": 1}])
        with self.assertRaises(AccessDenied):
            warehouse.query("delete from patients")

    def test_missing_data_fails_safe_and_monitoring_is_aggregate(self):
        service = DecisionService(ThresholdPolicy("deterioration", "model-v1", {"heart_rate": 1}, 5, 0.1), AuditLog(b"x" * 32))
        result = service.assess("patient-1", [], "clinician-1")
        recommendation = result["outputParameters"]["parameter"][0]["valueCode"]
        self.assertEqual(recommendation, "insufficient-data")

        monitor = ModelMonitor({"heart_rate": 80}, drift_limit=10)
        monitor.record_assessment([])
        monitor.record_review(False)
        self.assertEqual(monitor.report()["override-rate"], 1.0)
        self.assertEqual(validate_predictions([0.9, 0.1], [1, 0])["auroc"], 1.0)


if __name__ == "__main__":
    unittest.main()
