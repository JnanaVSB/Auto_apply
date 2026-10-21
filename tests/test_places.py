"""US or not, for single job locations, using the GeoNames place file shipped as app/places.json."""

import pytest

from app import places


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        # US
        ("Seattle", "us"),
        ("Austin", "us"),
        ("San Francisco, CA", "us"),
        ("New York", "us"),
        ("Washington, DC", "us"),
        ("Chicago, IL", "us"),
        ("Indianapolis, IN", "us"),
        ("Laurel, MD", "us"),
        ("Ontario, CA", "us"),  # Ontario, California
        ("Jordan, MN", "us"),  # not the country
        ("Boston, Massachusetts", "us"),
        ("Remote in USA", "us"),
        ("NYC", "us"),
        ("Hybrid - Austin, TX", "us"),
        ("Smallville, TX", "us"),  # unknown town, but TX is no country's code
        ("St Paul, MN", "us"),  # GeoNames: Saint Paul
        ("Lake St. Louis, MO", "us"),
        ("Cranberry Twp, PA", "us"),
        ("Arlington County, Arlington, VA", "us"),
        ("Research Triangle Park, Durham, NC", "us"),
        ("Silicon Valley, CA", "us"),
        # clearly foreign
        ("Toronto, CA", "foreign"),  # CA = Canada here
        ("Vancouver, BC, CA", "foreign"),
        ("Toronto, ON, Canada", "foreign"),
        ("Bengaluru, IN", "foreign"),
        ("Tel Aviv, IL", "foreign"),
        ("Berlin, DE", "foreign"),
        ("London, UK", "foreign"),
        ("Paris, France", "foreign"),
        ("Remote - Canada", "foreign"),
        ("Mexico City, Mexico", "foreign"),
        ("London", "foreign"),  # 8.9 million people vs London, Kentucky
        ("São Paulo, Brasil", "foreign"),
        ("Ontario", "foreign"),  # the province
        ("Tinytown, ON", "foreign"),  # unknown town, but ON is only ever Ontario
        ("Netherlands", "foreign"),
        ("Europe", "foreign"),
        ("Remote - EMEA", "foreign"),
        # kept for the user to decide
        ("Remote", "unclear"),
        ("Anywhere", "unclear"),
        ("", "unclear"),
        ("Vancouver", "unclear"),  # big in both Washington and British Columbia
        ("Cambridge", "unclear"),
        ("Tinyville, IN", "unclear"),  # unknown town; IN could be Indiana or India
        ("Clayton, IN", "unclear"),  # the big Clayton is in Australia, not India
        ("Qwertyville Base 7", "unclear"),  # a made-up name
    ],
)
def test_classify(place, expected):
    assert places.classify(place) == expected


def test_new_mexico_is_not_mexico():
    assert places.classify("Albuquerque, New Mexico") == "us"
    assert places.classify("Santa Fe, New Mexico") == "us"


def test_the_place_file_is_complete():
    assert len(places.US_CITIES) > 10_000 and len(places.FOREIGN_CITIES) > 10_000
    assert places.COUNTRIES["canada"] == "CA"
    assert "united states" not in places.COUNTRIES
    assert "GeoNames" in places._PLACES["source"]
