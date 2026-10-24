from src.expert_engine import load_expertise
from src.normalizer import normalize, parse_rate_per_hour
from src.output_validator import unapproved_dangerous_actions, validate_and_guard
from src.pipeline import prepare_expert
from src.schemas import Action, Report

GIB = 2**30


def test_concept_example_tte_8h_high():
    _, risk, _, _ = prepare_expert({"labels": {"alertname": "MongoDBDiskSpaceLow"}, "observed_metrics": {
        "filesystem_size_bytes": "500GiB", "filesystem_available_bytes": "40GiB", "growth_rate_per_hour": "5GiB/h"}})
    assert risk.time_to_exhaustion_hours == 8.0
    assert risk.urgency == "high"


def test_flat_95_percent_is_not_urgent():
    _, risk, _, _ = prepare_expert({"observed_metrics": {"size": "1TiB", "available": "51GiB", "growth_rate": "0GiB/h"}})
    assert risk.time_to_exhaustion_hours is None
    assert risk.urgency in {"low", "medium"}


def test_percentage_only_gives_unknown():
    _, risk, _, _ = prepare_expert("disk alert on db-7, 85% used")
    assert risk.time_to_exhaustion_hours is None
    assert risk.urgency == "unknown"


def test_inode_text_not_confused_with_disk_usage():
    a = normalize("web-03: inodes 99% used, disk usage 40%")
    assert a.category == "inode_exhaustion"
    assert a.metrics["used_pct"] == 40.0


def test_rate_units():
    assert parse_rate_per_hour("120 MiB/min") == 120 * 2**20 * 60
    assert parse_rate_per_hour("24GiB/day") == GIB


def test_guard_forces_approval_and_drops_fake_rules():
    exp = load_expertise()
    _, risk, rules, _ = prepare_expert({"labels": {"alertname": "MongoDBDiskSpaceLow"}, "observed_metrics": {
        "size": "500GiB", "available": "40GiB", "growth_rate": "5GiB/h"}})
    rep = Report(immediate_actions=[Action(action="Run compact on the events collection", requires_approval=False)],
                 applied_expert_rules=["made.up.001"], assessment={"urgency": "low"})
    assert unapproved_dangerous_actions(rep, exp)
    rep, log = validate_and_guard(rep, risk, exp, {r.id for r in rules})
    assert rep.immediate_actions[0].requires_approval
    assert "made.up.001" not in rep.applied_expert_rules
    assert rep.assessment.urgency == "high"
    assert not unapproved_dangerous_actions(rep, exp)


def test_context_and_impact_extracted_from_text():
    a = normalize("Disk 90% used. Release 4.12 went out 3 hours ago. Customers are complaining about timeouts.")
    assert "4.12" in a.metrics["recent_changes"]
    assert a.metrics["user_impact"] is True
    _, risk, _, _ = prepare_expert("Disk 90% used. Release 4.12 went out 3 hours ago. Customers are complaining about timeouts.")
    assert risk.urgency == "critical"


def test_impact_and_changes_are_required_context():
    _, risk, rules, _ = prepare_expert("disk alert on db-7")
    ids = {r.id for r in rules}
    assert {"global.impact.001", "global.harm.001", "global.context.001"} <= ids
    assert {"user_impact", "recent_changes"} <= set(risk.missing_context)


def test_feedback_roundtrip_and_bad_outcome_guard(tmp_path):
    from src import feedback
    store = tmp_path / "incidents.jsonl"
    alert = normalize({"labels": {"alertname": "MongoDBDiskSpaceLow", "instance": "mongo-x:9216"}})
    feedback.record(alert, "high", ["Run compact on the primary"], "made_worse", "I/O spike", [], path=store)
    items = feedback.load_all(path_seed=tmp_path / "none.jsonl", path_store=store)
    assert feedback.similar(alert, items)[0].outcome == "made_worse"
    _, risk, rules, _ = prepare_expert({"labels": {"alertname": "MongoDBDiskSpaceLow"}, "observed_metrics": {
        "size": "500GiB", "available": "40GiB", "growth_rate": "5GiB/h"}})
    rep = Report(immediate_actions=[Action(action="Run compact on primary node", requires_approval=True)])
    rep, log = validate_and_guard(rep, risk, load_expertise(), {r.id for r in rules}, items)
    assert "MADE IT WORSE" in rep.immediate_actions[0].guard_note
