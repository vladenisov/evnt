from evnt.tracker import utils as utils_module


find_available = utils_module.find_available


def test_find_available_returns_existing_dict_without_json_decoding():
    result = find_available({"data": []}, None)

    assert result == {"data": []}


def test_find_available_returns_none_for_invalid_json():
    result = find_available("not-json", None)

    assert result is None
