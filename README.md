# medjev
Jev at the hospital 🏥

## Clinical decision-support foundation

MedJev is a small, dependency-free foundation for hospital decision support over FHIR and data-warehouse data. It is **not** a diagnostic device and must not be used for autonomous clinical decisions. Every recommendation requires documented human review.

### Included capabilities

- HTTPS-only FHIR reads limited to an allow-list of resource types.
- Single-statement, parameterized, read-only warehouse queries.
- Versioned terminology mapping from coded FHIR Observations to features, retaining source provenance.
- Versioned threshold-policy classification with confidence and understandable feature contributions.
- FHIR R4 `GuidanceResponse` output, explicit human-review requirement, and source references.
- Pseudonymized audit events, including clinician acceptance/override and rationale.
- Aggregate drift and review/override monitoring plus retrospective AUROC and Brier-score validation helpers.

### Initial use case and success criteria

The initial supported pattern is **deterioration-risk triage** for a clinician reviewing a patient record. A policy may raise `review-required`, but it cannot create orders or replace professional judgement. Before enabling a policy, governance must set its target population, exclusion criteria, clinical owner, escalation workflow, threshold, and measurable acceptance criteria (calibration, subgroup performance, alert burden, and harmful-event limits).

### Governance before use

1. Define one narrowly scoped use case, intended users, outcome, alert threshold, escalation route, and contraindications with clinical governance.
2. Use a service account with the minimum FHIR scopes and warehouse `SELECT` privilege; store its credentials in a secrets manager, never configuration or source.
3. Validate the feature mapping and model retrospectively for discrimination, calibration, subgroup fairness, missing-data behavior, and privacy risk. Obtain institutional approval before prospective use.
4. Persist audit events in an access-controlled, retention-managed store; integrate clinician review into the workflow.
5. Monitor data completeness/distribution drift, model performance and calibration, alert burden, overrides, and safety events. Version every mapping and policy; disable or roll back a policy on unsafe behavior.

### Quick check

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

The supplied `ThresholdPolicy` is a transparent baseline. Production models should be externally validated, versioned, monitored, and served behind authenticated, authorized APIs.
