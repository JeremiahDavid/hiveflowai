from hiveflow.profiling import detect_patterns, infer_type, profile_column


def test_infer_type_number() -> None:
    assert infer_type([1, 2, 3]) == "number"


def test_infer_type_boolean() -> None:
    assert infer_type([True, False, True]) == "boolean"


def test_infer_type_email() -> None:
    assert infer_type(["a@example.com", "b@example.com"]) == "email"


def test_infer_type_date() -> None:
    assert infer_type(["2024-01-01", "2024-02-15"]) == "date"


def test_infer_type_currency() -> None:
    assert infer_type(["$1,200.50", "$99.00"]) == "currency"


def test_infer_type_string_fallback() -> None:
    assert infer_type(["alpha", "beta", "gamma"]) == "string"


def test_infer_type_empty_is_unknown() -> None:
    assert infer_type([None, "", None]) == "unknown"


def test_detect_patterns_email() -> None:
    assert detect_patterns(["a@example.com", "b@example.com"]) == ["email"]


def test_detect_patterns_none_for_mixed_values() -> None:
    assert detect_patterns(["alpha", "beta"]) == []


def test_profile_column_likely_key() -> None:
    profile = profile_column("customer_id", ["1", "2", "3", "4"])
    assert profile["name"] == "customer_id"
    assert profile["cardinality"] == 4
    assert profile["unique_ratio"] == 1.0
    assert profile["likely_key"] is True
    assert profile["null_rate"] == 0.0


def test_profile_column_null_rate_and_low_cardinality() -> None:
    profile = profile_column("status", ["open", "open", None, "closed"])
    assert profile["null_rate"] == 0.25
    assert profile["likely_key"] is False
    assert profile["cardinality"] == 2
    assert {"value": "open", "count": 2} in profile["top_values"]


def test_profile_column_all_null() -> None:
    profile = profile_column("empty_col", [None, None])
    assert profile["null_rate"] == 1.0
    assert profile["inferred_type"] == "unknown"
    assert profile["likely_key"] is False
    assert profile["sample_values"] == []
