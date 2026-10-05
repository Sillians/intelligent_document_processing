"""Verify persisted review and OCR exception replay against configured Postgres."""
import json
from human_review_console.app.decision_repository import ReviewDecisionRepository
from shared.idp_common.config import get_settings


def main():
    repository = ReviewDecisionRepository(get_settings())
    try:
        for task, decision in (("review-test", "approved"), ("ocr-exception", "exception")):
            record = {"tenant_id": "verification", "job_id": "recovery-test", "review_task_id": task,
                      "decision": decision, "fields": {"total_amount": "42.00"}, "reviewer_id": "verification-user"}
            first = repository.create(record)
            replay = repository.create({**record, "fields": {"total_amount": "99.00"}})
            assert replay == first, "Replay overwrote the committed decision"
            assert repository.get("verification", "recovery-test", task) == first
            assert repository.get("other-tenant", "recovery-test", task) is None
        print(json.dumps({"passed": True, "checks": ["immutable replay", "review persistence", "exception persistence", "tenant isolation"]}))
    finally:
        repository.close()


if __name__ == "__main__":
    main()
