import pytest

from ai_observability_bot.parser.alert_parser import Alert, AlertStatus
from ai_observability_bot.parser.filtering import FilterCondition, IgnoreRegistry, IgnoreRule


@pytest.fixture
def sample_alert():
    return Alert(
        status=AlertStatus.FIRING,
        alertname="TestAlert",
        cluster="prod",
        namespace="default",
        severity="critical",
        labels={
            "alertname": "TestAlert",
            "cluster": "prod",
            "namespace": "default",
            "severity": "critical",
            "job": "test",
        },
        annotations={},
        fingerprint="test-fp",
    )


def test_filter_condition_labels(sample_alert):
    cond = FilterCondition(labels={"alertname": "TestAlert", "cluster": "prod"})
    assert cond.matches(sample_alert) is True

    cond = FilterCondition(labels={"alertname": "Other"})
    assert cond.matches(sample_alert) is False


def test_filter_condition_any(sample_alert):
    # OR logic: cluster is 'dev' OR namespace is 'default'
    cond = FilterCondition(
        any=[
            FilterCondition(labels={"cluster": "dev"}),
            FilterCondition(labels={"namespace": "default"}),
        ]
    )
    assert cond.matches(sample_alert) is True

    # OR logic: cluster is 'dev' OR namespace is 'other'
    cond = FilterCondition(
        any=[
            FilterCondition(labels={"cluster": "dev"}),
            FilterCondition(labels={"namespace": "other"}),
        ]
    )
    assert cond.matches(sample_alert) is False


def test_filter_condition_all(sample_alert):
    # AND logic: cluster is 'prod' AND severity is 'critical'
    cond = FilterCondition(
        all=[
            FilterCondition(labels={"cluster": "prod"}),
            FilterCondition(labels={"severity": "critical"}),
        ]
    )
    assert cond.matches(sample_alert) is True

    # AND logic: cluster is 'prod' AND severity is 'info'
    cond = FilterCondition(
        all=[
            FilterCondition(labels={"cluster": "prod"}),
            FilterCondition(labels={"severity": "info"}),
        ]
    )
    assert cond.matches(sample_alert) is False


def test_ignore_registry_logic(sample_alert):
    rule1 = IgnoreRule(name="Rule 1", condition=FilterCondition(labels={"alertname": "Other"}))
    rule2 = IgnoreRule(name="Rule 2", condition=FilterCondition(labels={"cluster": "prod"}))

    registry = IgnoreRegistry(rules=[rule1, rule2])
    assert registry.should_ignore(sample_alert) is True

    registry = IgnoreRegistry(rules=[rule1])
    assert registry.should_ignore(sample_alert) is False
def test_not_labels_condition():
    from ai_observability_bot.parser.filtering import FilterCondition
    from ai_observability_bot.parser.alert_parser import Alert

    alert_prod = Alert(
        status="firing",
        alertname="TestAlert",
        cluster="prod",
        namespace="default",
        severity="critical",
        labels={"cluster": "prod"},
        annotations={},
        startsAt="2023-01-01T00:00:00Z",
        endsAt="0001-01-01T00:00:00Z",
        generatorURL="",
        fingerprint="fp1",
    )
    alert_dev = Alert(
        status="firing",
        alertname="TestAlert",
        cluster="dev",
        namespace="default",
        severity="critical",
        labels={"cluster": "dev"},
        annotations={},
        startsAt="2023-01-01T00:00:00Z",
        endsAt="0001-01-01T00:00:00Z",
        generatorURL="",
        fingerprint="fp2",
    )
    alert_no_cluster = Alert(
        status="firing",
        alertname="TestAlert",
        cluster="unknown",
        namespace="default",
        severity="critical",
        labels={},
        annotations={},
        startsAt="2023-01-01T00:00:00Z",
        endsAt="0001-01-01T00:00:00Z",
        generatorURL="",
        fingerprint="fp3",
    )

    # Condition: ignore if cluster is NOT "prod" (meaning we WANT prod)
    cond = FilterCondition(not_labels={"cluster": "prod"})
    
    assert cond.matches(alert_dev) is True # "dev" is not "prod", so condition matches (ignore)
    assert cond.matches(alert_no_cluster) is True # None is not "prod", so condition matches (ignore)
    assert cond.matches(alert_prod) is False # "prod" IS "prod", so condition does NOT match (don't ignore)
