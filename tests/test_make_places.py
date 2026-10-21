"""The place-file builder, on a tiny made-up extract in GeoNames' own file layout."""

import json

from tools import make_places


def city(name, country, state, population):
    columns = ["1", name, name, "", "0", "0", "P", "PPL", country, "", state, "", "", "", str(population)]
    return "\t".join(columns + ["", "", "UTC", "2026-01-01"])


def test_builds_us_and_foreign_cities_and_countries(tmp_path, capsys):
    rows = [
        city("Springfield", "US", "IL", 114_000),
        city("Springfield", "US", "MA", 155_000),
        city("Toronto", "CA", "08", 2_794_356),
        city("Tiny Village", "FR", "11", 900),  # too small abroad to keep
    ]
    (tmp_path / "cities1000.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
    countries = ["#ISO\tISO3\tnum\tfips\tCountry", "CA\tCAN\t124\tCA\tCanada", "US\tUSA\t840\tUS\tUnited States"]
    (tmp_path / "countryInfo.txt").write_text("\n".join(countries) + "\n", encoding="utf-8")
    make_places.main(tmp_path)
    places = json.loads(capsys.readouterr().out)
    assert places["us_cities"] == {"springfield": [155_000, ["IL", "MA"]]}
    assert places["foreign_cities"] == {"toronto": [2_794_356, ["CA"]]}
    assert places["countries"] == {"canada": "CA"}  # the US itself is never a "foreign" country
    assert "CC BY 4.0" in places["source"]
