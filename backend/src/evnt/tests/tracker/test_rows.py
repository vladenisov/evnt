"""Row building in ``evnt.tracker.rows``: unpaired surrogates never reach a sink."""

from ipaddress import IPv4Address

import orjson
import pytest

from evnt.tracker.element import EventIssues
from evnt.tracker.models import PayloadElementModel, UserAgentModel
from evnt.tracker.rows import (
    apply_platform,
    build_fallback_model,
    build_initial_model,
    dump_insert_model,
    keep_replaced_urls,
    scrub_surrogates,
)

_IP = IPv4Address("203.0.113.7")
HIGH = "\ud83d"
LOW = "\ude00"


def _model(**params):
    element = PayloadElementModel.model_construct(
        e="pv", aid="app", p="web", tv="js-3.0.0", res="1x1", **params
    )
    return build_initial_model(element, UserAgentModel(), _IP)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"x{HIGH}y", "x�y"),
        (f"x{LOW}y", "x�y"),
        (HIGH, "�"),
        (f"{LOW}{HIGH}", "��"),
        (f"café {HIGH}", "café �"),
    ],
)
def test_an_unpaired_surrogate_becomes_the_replacement_character(value, expected):
    scrubbed = scrub_surrogates(value)

    assert scrubbed == expected
    scrubbed.encode("utf-8")


@pytest.mark.parametrize(
    "value",
    ["", "plain ascii", "café", "привет", "ok \U0001f600", "�"],
)
def test_a_clean_string_is_returned_as_is(value):
    assert scrub_surrogates(value) is value


def test_a_pair_decoded_from_json_is_one_character_and_stays():
    emoji = orjson.loads(b'"\\ud83d\\ude00"')

    assert emoji == "\U0001f600"
    assert scrub_surrogates(emoji) is emoji


@pytest.mark.parametrize(
    "value",
    [None, True, 7, 1.5, b"\xed\xa0\x80", _IP, frozenset({"a"})],
)
def test_a_value_that_is_not_text_is_returned_as_is(value):
    assert scrub_surrogates(value) is value


def test_nested_values_and_keys_are_scrubbed():
    value = {
        f"k{HIGH}": f"v{LOW}",
        "list": ["a", f"b{HIGH}", {"deep": (f"c{LOW}", 3)}],
        "n": 1,
    }

    scrubbed = scrub_surrogates(value)

    assert scrubbed == {
        "k�": "v�",
        "list": ["a", "b�", {"deep": ("c�", 3)}],
        "n": 1,
    }
    assert list(scrubbed) == ["k�", "list", "n"]
    orjson.dumps(scrubbed)


def test_scrubbing_leaves_the_original_containers_untouched():
    inner = [f"b{HIGH}"]
    value = {"list": inner}

    scrubbed = scrub_surrogates(value)

    assert scrubbed is not value
    assert value == {"list": [f"b{HIGH}"]}
    assert value["list"] is inner


def test_clean_containers_are_not_copied():
    clean_branch = {"a": ["x", {"y": "é"}]}
    value = {"clean": clean_branch, "dirty": [f"z{HIGH}"], "tuple": ("a", 1)}

    assert scrub_surrogates(clean_branch) is clean_branch
    scrubbed = scrub_surrogates(value)
    assert scrubbed["clean"] is clean_branch
    assert scrubbed["tuple"] is value["tuple"]
    assert scrubbed["dirty"] == ["z�"]


def test_keys_that_become_equal_keep_the_later_value():
    assert scrub_surrogates({f"k{HIGH}": 1, f"k{LOW}": 2}) == {"k�": 2}


def test_dump_scrubs_strings_and_nested_values_of_the_row():
    model = _model(page=f"title {HIGH}", url="https://example.com/é")
    model.se_pr = {f"k{LOW}": [f"v{HIGH}"]}

    row = dump_insert_model(model)

    assert row["page"] == "title �"
    assert row["se_pr"] == {"k�": ["v�"]}
    assert row["url"] == "https://example.com/é"
    assert row["user_ip"] is _IP
    # The model keeps what it held: only the row is cleaned.
    assert model.page == f"title {HIGH}"
    orjson.dumps(row, default=str)


def test_dump_of_a_clean_row_shares_every_value_with_the_model():
    model = _model(page="привет \U0001f600")
    model.extra = {"static": {"k": ["v", "é"]}}

    row = dump_insert_model(model)

    assert row is not model.__dict__
    assert row.keys() == model.__dict__.keys()
    assert all(row[name] is value for name, value in model.__dict__.items())


def test_dump_notes_each_scrubbed_column_when_given_issues():
    model = _model(page=f"x{HIGH}", refr="clean")
    model.se_pr = {"k": f"v{LOW}"}
    issues = EventIssues()

    dump_insert_model(model, issues)

    note = "unpaired surrogate replaced with U+FFFD"
    assert sorted(issues.validation, key=lambda entry: entry["field"]) == [
        {"stage": "payload", "field": "page", "error": note},
        {"stage": "payload", "field": "se_pr", "error": note},
    ]


def test_dump_of_a_clean_row_notes_nothing():
    issues = EventIssues()

    dump_insert_model(_model(page="café"), issues)

    assert issues.validation is None


def test_a_replaced_url_is_kept_without_its_surrogates():
    element = PayloadElementModel.model_construct(
        url=f"myapp://deep/{HIGH}", refr=f"myapp://from/{LOW}"
    )
    result = _model(url="Home", refr="Splash")
    issues = EventIssues()

    keep_replaced_urls(element, result, issues)

    assert issues.tracker == {"url": "myapp://deep/�", "refr": "myapp://from/�"}


def test_a_rejected_platform_is_kept_without_its_surrogates():
    issues = EventIssues()
    issues.track("platform", f"web{HIGH}")

    apply_platform(_model(), issues)

    assert issues.tracker == {"platform": "web�"}


def test_a_clean_rejected_platform_is_kept_as_sent():
    sent = "приставка"
    issues = EventIssues()
    issues.track("platform", sent)
    issues.track("url", "myapp://deep")

    apply_platform(_model(), issues)

    assert issues.tracker["platform"] is sent
    assert issues.tracker["url"] == "myapp://deep"


def test_a_platform_without_a_column_code_is_stored_as_the_closest_one():
    issues = EventIssues()
    model = _model()
    model.p = "headset"

    apply_platform(model, issues)

    assert model.p == "mob"
    assert issues.tracker == {"platform": "headset"}


def test_the_fallback_row_keeps_a_rejected_platform_without_its_surrogates():
    element = PayloadElementModel.model_construct(
        e="pv", aid="app", p="web", tv="js-3.0.0", res="1x1"
    )
    issues = EventIssues()
    issues.track("platform", f"{LOW}tv")

    build_fallback_model(element, UserAgentModel().__dict__, _IP, issues)

    assert issues.tracker == {"platform": "�tv"}
