"""Pure-function tests for the PushPress title parser."""

import pytest

from pushpress import parse_title


@pytest.mark.parametrize(
    "title, expected_code, expected_category",
    [
        ("OV | Classic CrossFit", "OV", "Classic CrossFit"),
        ("HW | Functional CrossFit", "HW", "Functional CrossFit"),
        ("KA | Functional CrossFit", "KA", "Functional CrossFit"),
        ("HW | Open Gym Back Hall 6", "HW", "Open Gym"),
        ("HW | Open Gym Hall 4 all memberships", "HW", "Open Gym"),
        ("KA | Open Gym", "KA", "Open Gym"),
        ("OV | Hyrox Open", "OV", "Hyrox Open"),
        ("Trial Class | Kanaalweg 29c", "", "Trial Class"),
        ("Trial class | CrossFit | Overste den Oudenlaan 9", "", "Trial Class"),
    ],
)
def test_parse_title(title, expected_code, expected_category):
    code, cat = parse_title(title)
    assert code == expected_code
    assert cat == expected_category
