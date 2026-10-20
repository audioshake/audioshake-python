import pytest

from audioshake import ValidationError, expand_models


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["vocals", "drums"], ["vocals", "drums"]),
        (["vocals, drums", " bass "], ["vocals", "drums", "bass"]),
        ("vocals", ["vocals"]),  # a str is iterable: must not become v,o,c,a,l,s
        ("vocals,drums", ["vocals", "drums"]),
    ],
    ids=["list", "comma-lists-and-whitespace", "one-string", "one-comma-list"],
)
def test_values_are_normalised_into_one_list(values, expected):
    assert expand_models(values) == expected


@pytest.mark.parametrize(
    "values", [["vocals,vocals"], ["vocals", "vocals"]], ids=["in-one-entry", "across-entries"]
)
def test_duplicates_are_rejected(values):
    with pytest.raises(ValidationError, match="more than once"):
        expand_models(values)


@pytest.mark.parametrize("values", [[""], ["vocals,,drums"]], ids=["empty", "empty-entry"])
def test_empty_input_is_rejected(values):
    with pytest.raises(ValidationError):
        expand_models(values)
