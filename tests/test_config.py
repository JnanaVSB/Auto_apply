from pathlib import Path

import pytest

from app.config import PROJECT_DIR, AIRole, AISettings, ConfigError, CustomMember, load_config

FAKE_VALUE = "fake-private-value-123"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_relative_data_dir_counts_from_project_folder(tmp_path):
    assert load_config(write(tmp_path, "data_dir: data\n")).data_dir == PROJECT_DIR / "data"


def test_absolute_data_dir_is_kept(tmp_path):
    assert load_config(write(tmp_path, f"data_dir: {tmp_path}\n")).data_dir == tmp_path.resolve()


def test_missing_file_gives_clear_message(tmp_path):
    with pytest.raises(ConfigError, match="Copy config.example.yaml"):
        load_config(tmp_path / "missing.yaml")


def test_missing_data_dir_is_reported_by_name(tmp_path):
    with pytest.raises(ConfigError, match="data_dir: Field required"):
        load_config(write(tmp_path, ""))


def test_unknown_setting_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="data_dri: Extra inputs are not permitted"):
        load_config(write(tmp_path, "data_dir: data\ndata_dri: data\n"))


@pytest.mark.parametrize("text", [f"data_dir: data\nsurprise: {FAKE_VALUE}\n", f"data_dir: [{FAKE_VALUE}\n"])
def test_errors_never_contain_values(tmp_path, text):
    with pytest.raises(ConfigError) as error:
        load_config(write(tmp_path, text))
    assert FAKE_VALUE not in str(error.value)
    assert error.value.__suppress_context__  # the original error, which may hold values, is not shown


@pytest.mark.parametrize("value", ['""', ".", '"~"', '"~/"', "..", '"/"'])
def test_data_dir_cannot_be_the_project_or_home_folder_or_above(tmp_path, value):
    with pytest.raises(ConfigError, match="a folder of its own"):
        load_config(write(tmp_path, f"data_dir: {value}\n"))


SECTIONS = """data_dir: data
roles:
  simplify_categories: ["AI/ML/Data"]
  keywords: ["machine learning", "RL"]
sources:
  simplify_lists:
    - url: https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/listings.json
      level: intern
  extra_boards: [" Greenhouse:Acme ", "ashby:globex"]
  phonebook_lists: ["https://raw.githubusercontent.com/someone/jobs/main/README.md"]
"""


def test_roles_and_sources_are_read(tmp_path):
    config = load_config(write(tmp_path, SECTIONS))
    assert config.roles.keywords == ["machine learning", "RL"]
    assert config.sources.simplify_lists[0].level == "intern"
    assert config.sources.extra_boards == ["greenhouse:acme", "ashby:globex"]
    assert config.sources.phonebook_lists == ["https://raw.githubusercontent.com/someone/jobs/main/README.md"]


def test_roles_and_sources_are_optional(tmp_path):
    config = load_config(write(tmp_path, "data_dir: data\n"))
    assert (config.roles.keywords, config.sources.simplify_lists, config.sources.extra_boards) == ([], [], [])


@pytest.mark.parametrize(
    ("old", "new", "where"),
    [
        ("https://raw.githubusercontent.com", "http://raw.githubusercontent.com", "simplify_lists.0.url"),
        ("https://raw.githubusercontent.com", f"https://{FAKE_VALUE}.example", "simplify_lists.0.url"),
        ("level: intern", "level: senior", "simplify_lists.0.level"),
        ('"ashby:globex"', f'"workday:{FAKE_VALUE}"', "extra_boards"),
        ('"ashby:globex"', '"lever:../admin"', "extra_boards"),
        ("https://raw.githubusercontent.com/someone", f"https://{FAKE_VALUE}.example/someone", "phonebook_lists.0"),
    ],
)
def test_bad_source_settings_are_named_without_their_value(tmp_path, old, new, where):
    with pytest.raises(ConfigError, match=f"sources.{where}") as error:
        load_config(write(tmp_path, SECTIONS.replace(old, new)))
    assert FAKE_VALUE not in str(error.value)


def test_the_example_settings_are_valid():
    config = load_config(PROJECT_DIR / "config.example.yaml")
    assert config.roles.keywords
    assert [listing.level for listing in config.sources.simplify_lists] == ["intern", "new grad"]


@pytest.mark.parametrize(("value", "ok"), [("0", True), ("168", True), ("-1", False), ("169", False),
                                            ('"6"', False), ("true", False), ("1.5", False)])  # fmt: skip
def test_the_dashboard_schedule_is_0_to_168_hours(tmp_path, value, ok):
    path = write(tmp_path, f"data_dir: data\ncollect_every_hours: {value}\n")
    if ok:
        assert load_config(path).collect_every_hours == int(value)
    else:
        with pytest.raises(ConfigError, match="collect_every_hours"):
            load_config(path)


@pytest.mark.parametrize(
    ("setting", "value", "ok"),
    [
        ("phonebook_every_days", "0", True), ("phonebook_every_days", "365", True),
        ("phonebook_every_days", "366", False), ("phonebook_every_days", "-1", False),
        ("tracker_stale_days", "1", True), ("tracker_stale_days", "365", True),
        ("tracker_stale_days", "0", False), ("tracker_stale_days", "366", False),
        ("gmail_lookback_days", "1", True), ("gmail_lookback_days", "366", False),
        ("gmail_every_minutes", "0", True), ("gmail_every_minutes", "5", True), ("gmail_every_minutes", "1", False),
        ("gmail_every_minutes", "4", False), ("gmail_every_minutes", "1441", False),
        ("requests_per_second", "1", True), ("requests_per_second", "5", True),
        ("requests_per_second", "0", False), ("requests_per_second", "6", False),
        ("requests_per_second", "0.5", False), ("requests_per_second", '"2"', False),
    ],
)  # fmt: skip
def test_the_phone_book_schedule_and_requests_per_second_have_limits(tmp_path, setting, value, ok):
    path = write(tmp_path, f"data_dir: data\n{setting}: {value}\n")
    if ok:
        assert getattr(load_config(path), setting) == int(value)
    else:
        with pytest.raises(ConfigError, match=setting):
            load_config(path)


def test_without_them_the_phone_book_is_monthly_at_one_request_per_second(tmp_path):
    config = load_config(write(tmp_path, "data_dir: data\n"))
    assert (config.phonebook_every_days, config.requests_per_second, config.tracker_stale_days) == (30, 1, 21)
    assert (config.gmail_lookback_days, config.gmail_every_minutes) == (60, 30)


def test_scoring_settings_have_defaults_and_limits(tmp_path):
    assert load_config(write(tmp_path, "data_dir: data\n")).scoring.meaning_threshold == 0.5
    with pytest.raises(ConfigError, match="scoring.skills_threshold: Input should be less than or equal to 1"):
        load_config(write(tmp_path, "data_dir: data\nscoring:\n  skills_threshold: 1.5\n"))


AI_ROLE = "data_dir: data\nai:\n  evaluator:\n"


@pytest.mark.parametrize(
    ("role", "problem"),
    [
        ("    model: m\n    address: http://192.168.1.5:11434\n", "Ollama must run on this computer"),
        ("    model: m\n    address: http://0.0.0.0:11434\n", "Ollama must run on this computer"),
        ("    service: anthropic\n    model: m\n    address: http://api.anthropic.com\n    key_name: k\n", "https://"),
        ("    service: anthropic\n    model: m\n", "needs key_name"),
        ("    service: openai-compatible\n    model: m\n    key_name: k\n", "https://"),  # no default address
        # plain http only for your own address, on a private network written as numbers
        ("    service: openai-compatible\n    model: m\n    address: http://8.8.8.8:8000/v1\n", "https://"),
        ("    service: openai-compatible\n    model: m\n    address: http://myserver.local/v1\n", "https://"),
        ("    service: openai-compatible\n    model: m\n    address: http://172.32.0.1/v1\n", "https://"),
        (
            "    service: openai-compatible\n    model: m\n    address: http://api.openai.com/v1\n    key_name: k\n",
            "https://",
        ),
        ("    service: anthropic\n    model: m\n    address: http://192.168.1.5\n    key_name: k\n", "https://"),
        ("    service: openai-compatible\n    model: m\n    address: https://u:p@example.org/v1\n", "no name or"),
        ("    service: openai-compatible\n    model: m\n    address: https://api.openai.com/v1\n", "needs key_name"),
        (
            "    service: openai-compatible\n    model: m\n    address: https://example.org/v1\n    price_input: 1\n",
            "give both prices",
        ),
        (
            "    service: anthropic\n    model: m\n    key_name: k\n    price_input: 1\n    price_output: 2\n",
            "only for your own address",
        ),
        (
            "    service: openai-compatible\n    model: m\n    address: https://example.org/v1\n    price_input: -1\n"
            "    price_output: 2\n",
            "greater than or equal to 0",
        ),
        ("    model: rm -rf /\n", "String should match pattern"),
        ("    model: m\n    thinking: off\n", "thinking: Input should be 'off'"),  # YAML reads a bare off as false
        ("    model: m\n    thinking: xhigh\n", "Anthropic and OpenAI models only"),
    ],
)
def test_ai_role_settings_are_checked(tmp_path, role, problem):
    with pytest.raises(ConfigError, match=problem):
        load_config(write(tmp_path, AI_ROLE + role))


def test_ai_roles_have_safe_defaults(tmp_path):
    ai = load_config(write(tmp_path, "data_dir: data\n")).ai
    role = ai.evaluator
    assert (role.service, role.model, role.address, role.thinking) == (
        "ollama", "gemma4:26b-a4b-it-q8_0", "http://127.0.0.1:11434", "off"
    )  # fmt: skip
    assert ai.writer is None  # no Writer until you choose one
    claude = load_config(write(tmp_path, AI_ROLE + "    service: anthropic\n    model: m\n    key_name: k\n"))
    assert claude.ai.evaluator.address == "https://api.anthropic.com"


@pytest.mark.parametrize(
    "address",
    ["http://192.168.1.5:8000/v1", "http://10.0.0.7/v1", "http://172.16.4.2/v1", "http://127.0.0.1:8080/v1",
     "http://100.101.102.103:8000/v1", "http://[fd12:3456::1]:8000/v1", "http://[::1]:8000/v1",
     "https://models.example.org/v1"],
)  # fmt: skip
def test_your_own_address_may_use_http_on_a_private_network_and_needs_no_key(tmp_path, address):
    own = f"data_dir: data\nai:\n  writer:\n    service: openai-compatible\n    model: m\n    address: {address}\n"
    role = load_config(write(tmp_path, own + "    price_input: 0.5\n    price_output: 1.5\n")).ai.writer
    assert (role.address, role.key_name, role.price_input, role.price_output) == (address, None, 0.5, 1.5)


def test_spending_limits_have_defaults_and_ranges(tmp_path):
    spending = load_config(write(tmp_path, "data_dir: data\n")).spending
    assert (spending.daily, spending.monthly) == (15, 150)
    for setting, value in (("daily", 0.5), ("daily", 501), ("monthly", 0), ("monthly", 5001)):
        with pytest.raises(ConfigError, match=f"spending.{setting}"):
            load_config(write(tmp_path, f"data_dir: data\nspending:\n  {setting}: {value}\n"))


LOCAL = "    service: ollama\n    model: gemma4:26b-a4b-it-q8_0\n"
COUNCIL = "".join(f"  {member}:\n{LOCAL}" for member in ("writer", "truth", "hiring", "editor", "ats", "domain"))


def test_the_council_has_defaults_and_ranges(tmp_path):
    council = load_config(write(tmp_path, "data_dir: data\n")).council
    assert (council.rounds, council.turns, council.daily_jobs) == (3, 1, 20)
    assert (council.automatic, council.start, council.end, council.any_time) == (False, "22:00", "08:00", False)
    for setting, value in (
        ("rounds", 0), ("rounds", 6), ("turns", -1), ("turns", 4), ("daily_jobs", 0), ("daily_jobs", 201),
        ("start", '"24:00"'), ("end", '"8:00"'), ("end", '"08:00; x"'),
    ):  # fmt: skip
        with pytest.raises(ConfigError, match=f"council.{setting}"):
            load_config(write(tmp_path, f"data_dir: data\ncouncil:\n  {setting}: {value}\n"))
    with pytest.raises(ConfigError, match="a start and an end that differ"):
        load_config(write(tmp_path, 'data_dir: data\ncouncil:\n  start: "08:00"\n'))
    hours = load_config(write(tmp_path, 'data_dir: data\ncouncil:\n  start: "08:00"\n  any_time: true\n')).council
    assert hours.any_time


def test_automatic_preparation_needs_the_writer_and_all_five_reviewers(tmp_path):
    on = "council:\n  automatic: true\n"
    config = load_config(write(tmp_path, f"data_dir: data\nai:\n{COUNCIL}{on}"))
    assert config.council.automatic and config.ai.council_chosen
    without_domain = COUNCIL.split("  domain:")[0]
    with pytest.raises(ConfigError, match="needs the Writer and all five council members"):
        load_config(write(tmp_path, f"data_dir: data\nai:\n{without_domain}{on}"))
    assert not load_config(write(tmp_path, f"data_dir: data\nai:\n{without_domain}")).ai.council_chosen


def test_added_members_have_their_own_id_name_and_model(tmp_path):
    member = "  - id: {}\n    name: {}\n    letters: true\n    role:\n  " + LOCAL.replace("\n    ", "\n      ")
    ai = "ai:\n  extra:\n" + member.format("x1", "Domain expert")
    extra = load_config(write(tmp_path, f"data_dir: data\n{ai}")).ai.extra
    assert [(added.id, added.name, added.letters, added.role.model) for added in extra] == [
        ("x1", "Domain expert", True, "gemma4:26b-a4b-it-q8_0")
    ]
    for added, problem in (
        (member.format("x1", "Domain expert") + member.format("x1", "Recruiter"), "its own id"),
        (member.format("x4", "Recruiter"), "ai.extra.0.id"),
        (member.format("x1", "Recruiter <b>"), "ai.extra.0.name"),
        ("".join(member.format(f"x{n}", "Recruiter") for n in (1, 2, 3, 3)), "at most 3 items"),
    ):
        with pytest.raises(ConfigError, match=problem):
            load_config(write(tmp_path, f"data_dir: data\nai:\n  extra:\n{added}"))


def test_the_council_members_are_the_five_reviewers_then_the_ones_you_added():
    laptop = AIRole(model="gemma4:26b-a4b-it-q8_0")
    ai = AISettings(truth=laptop, extra=[CustomMember(id="x2", name="Recruiter", role=laptop)])
    assert list(ai.members) == ["truth", "hiring", "editor", "ats", "domain", "x2"]
    assert ai.members["truth"] == ai.members["x2"] == laptop and ai.members["hiring"] is None
    assert ai.names == {"x2": "Recruiter"} and AISettings().names == {}


@pytest.mark.parametrize(
    ("setting", "value"), [("minutes_per_day", 0), ("minutes_per_day", 1441), ("ready_threshold", 101)]
)
def test_scoring_time_and_threshold_have_limits(tmp_path, setting, value):
    with pytest.raises(ConfigError, match=f"scoring.{setting}"):
        load_config(write(tmp_path, f"data_dir: data\nscoring:\n  {setting}: {value}\n"))
