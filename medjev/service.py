"""Auditable, human-in-the-loop clinical decision support building blocks."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccessDenied(ValueError):
    """Raised when a connector request violates its access policy."""


class FHIRClient:
    """FHIR R4 reader restricted to configured HTTPS endpoints and resources."""

    def __init__(self, base_url: str, bearer_token: str, allowed_resources: Sequence[str] = ("Patient", "Observation", "Condition", "MedicationRequest")):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise AccessDenied("FHIR base URL must be an absolute HTTPS URL")
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.allowed_resources = frozenset(allowed_resources)

    def read(self, resource_type: str, resource_id: str) -> Mapping[str, Any]:
        if resource_type not in self.allowed_resources:
            raise AccessDenied(f"FHIR resource {resource_type!r} is not permitted")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", resource_id):
            raise AccessDenied("invalid FHIR resource ID")
        request = Request(
            f"{self.base_url}/{resource_type}/{resource_id}",
            headers={"Accept": "application/fhir+json", "Authorization": "Bearer " + self.bearer_token},
            method="GET",
        )
        with urlopen(request, timeout=10) as response:  # nosec B310: URL is policy-validated above
            return json.loads(response.read())


class ReadOnlyWarehouse:
    """Execute a single SELECT statement through a database connection factory."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]):
        self._connection_factory = connection_factory

    @staticmethod
    def _is_read_query(query: str) -> bool:
        cleaned = re.sub(r"/\*.*?\*/|--[^\n]*", "", query, flags=re.S).strip()
        return bool(re.fullmatch(r"(?:SELECT|WITH)\b[\s\S]*[^;]\s*", cleaned, re.I))

    def query(self, query: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        if not self._is_read_query(query):
            raise AccessDenied("warehouse permits one SELECT or WITH query only")
        connection = self._connection_factory()
        try:
            connection.set_authorizer(self._authorizer)
            cursor = connection.execute(query, parameters)
            columns = [item[0] for item in cursor.description or ()]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            connection.close()

    @staticmethod
    def _authorizer(action: int, arg1: str | None, arg2: str | None, database: str | None, source: str | None) -> int:
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


@dataclass(frozen=True)
class Feature:
    name: str
    value: float
    system: str
    code: str
    source_id: str
    observed_at: str | None


class FeatureNormalizer:
    """Maps coded FHIR Observations to a deliberately versioned feature set."""

    def __init__(self, mappings: Mapping[tuple[str, str], str], version: str):
        self.mappings = dict(mappings)
        self.version = version

    def normalize_observations(self, observations: Sequence[Mapping[str, Any]]) -> list[Feature]:
        features: list[Feature] = []
        for observation in observations:
            coding = (observation.get("code", {}).get("coding") or [{}])[0]
            key = (coding.get("system", ""), coding.get("code", ""))
            name = self.mappings.get(key)
            value = observation.get("valueQuantity", {}).get("value")
            if name and isinstance(value, (int, float)) and observation.get("id"):
                features.append(Feature(name, float(value), key[0], key[1], observation["id"], observation.get("effectiveDateTime")))
        return features


@dataclass(frozen=True)
class ThresholdPolicy:
    name: str
    version: str
    weights: Mapping[str, float]
    intercept: float
    threshold: float

    def evaluate(self, features: Sequence[Feature]) -> tuple[float, str, list[str]]:
        contributions = {feature.name: feature.value * self.weights[feature.name] for feature in features if feature.name in self.weights}
        score = self.intercept + sum(contributions.values())
        confidence = 1 / (1 + pow(2.718281828, -score))
        decision = "review-required" if confidence >= self.threshold else "no-alert"
        explanations = [f"{name}: {value:.3f}" for name, value in sorted(contributions.items(), key=lambda item: abs(item[1]), reverse=True)]
        return confidence, decision, explanations


class AuditLog:
    """In-memory audit log storing pseudonymous patient references, never raw FHIR."""

    def __init__(self, pseudonym_key: bytes):
        if len(pseudonym_key) < 32:
            raise ValueError("pseudonym key must contain at least 32 bytes")
        self._key = pseudonym_key
        self.events: list[dict[str, Any]] = []

    def patient_ref(self, patient_id: str) -> str:
        return hmac.new(self._key, patient_id.encode(), hashlib.sha256).hexdigest()

    def record(self, action: str, patient_id: str, actor: str, decision_id: str, details: Mapping[str, Any]) -> None:
        self.events.append({"at": _now(), "action": action, "patient": self.patient_ref(patient_id), "actor": actor, "decision_id": decision_id, "details": dict(details)})


class DecisionService:
    """Creates FHIR GuidanceResponse recommendations requiring clinician review."""

    def __init__(self, policy: ThresholdPolicy, audit_log: AuditLog):
        self.policy = policy
        self.audit_log = audit_log
        self.decisions: dict[str, dict[str, Any]] = {}

    def assess(self, patient_id: str, features: Sequence[Feature], actor: str) -> dict[str, Any]:
        if features:
            confidence, decision, explanations = self.policy.evaluate(features)
        else:
            confidence, decision, explanations = 0.0, "insufficient-data", ["No mapped features available"]
        decision_id = str(uuid.uuid4())
        response = {
            "resourceType": "GuidanceResponse",
            "id": decision_id,
            "status": "success",
            "subject": {"reference": f"Patient/{patient_id}"},
            "occurrenceDateTime": _now(),
            "moduleCanonical": f"urn:medjev:policy:{self.policy.name}|{self.policy.version}",
            "outputParameters": {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "recommendation", "valueCode": decision},
                    {"name": "confidence", "valueDecimal": round(confidence, 4)},
                    {"name": "requires-human-review", "valueBoolean": True},
                    {"name": "explanation", "valueString": "; ".join(explanations) or "No mapped features available"},
                    {"name": "feature-set-version", "valueString": self.policy.version},
                ],
            },
            "reasonCode": [{"text": "Decision support only; clinician confirmation is required."}],
            "dataRequirement": [{"type": "Observation", "profile": [f"Observation/{item.source_id}" for item in features]}],
        }
        self.decisions[decision_id] = response
        self.audit_log.record("assessed", patient_id, actor, decision_id, {"policy": self.policy.version, "decision": decision})
        return response

    def review(self, decision_id: str, patient_id: str, actor: str, accepted: bool, rationale: str) -> None:
        if decision_id not in self.decisions or not rationale.strip():
            raise ValueError("known decision ID and non-empty rationale are required")
        self.audit_log.record("reviewed", patient_id, actor, decision_id, {"accepted": accepted, "rationale": rationale[:1000]})


class ModelMonitor:
    """Tracks aggregate, non-identifying usage and feature distribution signals."""

    def __init__(self, baseline_means: Mapping[str, float], drift_limit: float = 0.25):
        self.baseline_means = dict(baseline_means)
        self.drift_limit = drift_limit
        self._values: dict[str, list[float]] = {name: [] for name in baseline_means}
        self.assessments = 0
        self.reviewed = 0
        self.overridden = 0

    def record_assessment(self, features: Sequence[Feature]) -> None:
        self.assessments += 1
        for feature in features:
            if feature.name in self._values:
                self._values[feature.name].append(feature.value)

    def record_review(self, accepted: bool) -> None:
        self.reviewed += 1
        self.overridden += int(not accepted)

    def report(self) -> dict[str, Any]:
        drift = {
            name: abs(sum(values) / len(values) - self.baseline_means[name]) > self.drift_limit
            for name, values in self._values.items() if values
        }
        return {
            "assessments": self.assessments,
            "review-rate": self.reviewed / self.assessments if self.assessments else 0.0,
            "override-rate": self.overridden / self.reviewed if self.reviewed else 0.0,
            "feature-drift": drift,
        }


def validate_predictions(predictions: Sequence[float], outcomes: Sequence[int]) -> dict[str, float]:
    """Return retrospective calibration and discrimination metrics for binary outcomes."""

    if not predictions or len(predictions) != len(outcomes) or any(value not in (0, 1) for value in outcomes):
        raise ValueError("non-empty, equally sized predictions and binary outcomes are required")
    brier = sum((prediction - outcome) ** 2 for prediction, outcome in zip(predictions, outcomes)) / len(outcomes)
    pairs = [(score, outcome) for score, outcome in zip(predictions, outcomes)]
    positives = [item for item in pairs if item[1] == 1]
    negatives = [item for item in pairs if item[1] == 0]
    if not positives or not negatives:
        raise ValueError("outcomes must include positive and negative cases")
    wins = sum(1 if positive[0] > negative[0] else 0.5 if positive[0] == negative[0] else 0 for positive in positives for negative in negatives)
    return {"brier-score": brier, "auroc": wins / (len(positives) * len(negatives))}
