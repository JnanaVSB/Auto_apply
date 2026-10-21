"""Rebuild app/places.json from GeoNames (https://www.geonames.org, CC BY 4.0).

Download these two files from https://download.geonames.org/export/dump/ into an empty folder
(unzip cities1000.zip there), then run from the project folder:

    python -I tools/make_places.py <folder> > app/places.json

The app itself never downloads anything for this; the file is committed so changes show up in git.
"""

import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

MIN_FOREIGN_POPULATION = 15_000  # smaller foreign towns are rarely job locations and only add size


def main(folder: Path) -> None:
    us_cities: dict[str, list] = {}
    foreign_cities: dict[str, list] = {}
    with open(folder / "cities1000.txt", encoding="utf-8", newline="") as file:
        for row in csv.reader(file, delimiter="\t", quoting=csv.QUOTE_NONE):
            names, country, state, population = {row[1].lower(), row[2].lower()}, row[8], row[10], int(row[14] or 0)
            if country != "US" and population < MIN_FOREIGN_POPULATION:
                continue
            for name in names:  # [largest population, the states (US) or countries that have this name]
                entry = (us_cities if country == "US" else foreign_cities).setdefault(name, [0, []])
                entry[0] = max(entry[0], population)
                entry[1] = sorted({*entry[1], state if country == "US" else country})
    countries = {}
    with open(folder / "countryInfo.txt", encoding="utf-8") as file:
        for line in file:
            fields = line.rstrip("\n").split("\t")
            if not line.startswith("#") and len(fields) > 4 and fields[0] != "US":
                countries[fields[4].lower()] = fields[0]
    places = {
        "source": f"GeoNames (https://www.geonames.org), CC BY 4.0; built {datetime.now(UTC).date()}",
        "countries": countries,
        "us_cities": us_cities,
        "foreign_cities": foreign_cities,
    }
    json.dump(places, sys.stdout, ensure_ascii=False, indent=0, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
