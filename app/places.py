"""Is a job location in the US? Place names come from GeoNames (app/places.json, CC BY 4.0)."""

import json
import re
from pathlib import Path

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}  # fmt: skip
PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon",
}  # fmt: skip
COUNTRY_ALIASES = {  # common names GeoNames writes differently
    "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB", "korea": "KR",
    "brasil": "BR", "holland": "NL", "netherlands": "NL", "uae": "AE", "deutschland": "DE",
}  # fmt: skip
FOREIGN_REGIONS = ("europe", "asia", "africa", "emea", "apac", "latam", "latin america", "south america", "oceania")
SHORT_FORMS = {"saint": "st", "fort": "ft", "mount": "mt", "township": "twp"}  # "St Paul" = "Saint Paul"
MAINLY = 10  # a bare city name counts for one side when that side's city has 10x the people of the other's


def _key(name: str) -> str:
    """A city name ignoring capitals, dots and short forms, so "St. Paul" finds "Saint Paul"."""
    return " ".join(SHORT_FORMS.get(word, word) for word in name.lower().replace(".", " ").split())


_PLACES = json.loads(Path(__file__).with_name("places.json").read_text(encoding="utf-8"))


def _cities(entries: dict[str, list]) -> dict[str, tuple[int, set[str]]]:
    """Name -> (largest population, states or countries with a city of that name), keyed by _key."""
    cities: dict[str, tuple[int, set[str]]] = {}
    for name, (population, areas) in entries.items():
        key = _key(name)
        people, known = cities.get(key, (0, set()))
        cities[key] = (max(people, population), known | set(areas))
    return cities


US_CITIES = _cities(_PLACES["us_cities"])
FOREIGN_CITIES = _cities(_PLACES["foreign_cities"])
NO_CITY = (0, set())
COUNTRIES: dict[str, str] = _PLACES["countries"] | COUNTRY_ALIASES  # lower-case name -> two-letter code
COUNTRY_CODES = {*COUNTRIES.values(), "UK"}


def whole_words(alternatives: str) -> re.Pattern:
    """A pattern for any of `alternatives` (a regex like "a|b") as whole words, ignoring capitals."""
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


US_WORDS = re.compile(
    r"\bunited states\b|\bsilicon valley\b"
    r"|(?-i:(?<![A-Za-z])(?:USA|U\.S\.A?|US|SF|NYC|LA|South SF|Bay Area)(?![A-Za-z]))",
    re.IGNORECASE,
)
STATE_NAMES = whole_words("|".join(map(re.escape, STATES.values())))
FOREIGN_NAMES = whole_words("|".join(map(re.escape, [*COUNTRIES, *PROVINCES.values(), *FOREIGN_REGIONS])))
FOREIGN_NAME_KEYS = {_key(name) for name in [*COUNTRIES, *PROVINCES.values(), *FOREIGN_REGIONS]}
REMOTE_ONLY = re.compile(r"(?:remote|anywhere|worldwide|global|[\s,;:/()–-])*", re.IGNORECASE)
CODE = re.compile(r"[A-Z]{2}")


def classify(place: str) -> str:
    """'us', 'foreign' (clearly not the US) or 'unclear' for one location, e.g. "Austin, TX" or "Toronto, CA"."""
    if US_WORDS.search(place):
        return "us"
    if REMOTE_ONLY.fullmatch(place):
        return "unclear"
    parts = [part.strip() for part in place.split(",") if part.strip()]
    city, codes = _key(parts[0]), [part for part in parts[1:] if CODE.fullmatch(part)]
    for name in (_key(part) for part in parts if not CODE.fullmatch(part)):  # "Arlington County, Arlington, VA"
        if any(code in US_CITIES.get(name, NO_CITY)[1] for code in codes):  # a US city in that state
            return "us"
    us, foreign = US_CITIES.get(city, NO_CITY), FOREIGN_CITIES.get(city, NO_CITY)
    if STATE_NAMES.search(place) and not (len(parts) > 1 and foreign[1] and not us[1]):  # not "Tbilisi, Georgia"
        return "us"
    for code in codes:
        if code in PROVINCES or (code in COUNTRY_CODES and (code not in STATES or code in foreign[1])):
            return "foreign"  # "Vancouver, BC", "London, UK", "Toronto, CA" (a Canadian city), "Bengaluru, IN"
        if code in STATES and code not in COUNTRY_CODES:
            return "us"  # a state code no country uses, e.g. "Smallville, TX"
    if len(parts) == 1 and us[1]:  # a whole US city name first: "Jersey City", "West Jordan"
        return "unclear" if city in FOREIGN_NAME_KEYS else _by_population(us, foreign)  # "Ontario" is both
    if FOREIGN_NAMES.search(place):
        return "foreign"
    return _by_population(us, foreign)


def _by_population(us: tuple[int, set[str]], foreign: tuple[int, set[str]]) -> str:
    """A bare city name belongs to the side whose city has 10x the people of the other's."""
    if us[1] and us[0] >= MAINLY * foreign[0]:
        return "us"  # "Seattle", "Austin"
    if foreign[1] and foreign[0] >= MAINLY * us[0]:
        return "foreign"  # "London" (8.9 million) rather than London, Kentucky
    return "unclear"  # "Vancouver", "Cambridge", or a place not recognised
