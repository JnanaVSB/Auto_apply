"""The plugin's own files, read as text: the manifest's rules, what no file may hold and what only one may. These guard
against a slip, not a determined change. Comments are taken out first, so a word in one hides and fails nothing."""

import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.resume import CONTROL
from app.sources import closed_site
from app.submit import ashby, fills, greenhouse, lever
from app.web import plugin

PLUGIN = Path(__file__).parents[1] / "plugin"
BACKGROUND, PAGE, POPUP, OPTIONS = "background.js", "page.js", "popup.js", "options.js"
HOSTS = {
    "https://job-boards.greenhouse.io/*",
    "https://jobs.lever.co/*",
    "https://jobs.ashbyhq.com/*",
    "http://127.0.0.1:8765/*",
}
FORM_PAGES = {
    "greenhouse": (greenhouse.FORM_PAGE, "https://job-boards.greenhouse.io/embed/job_app*"),
    "lever": (lever.FORM_PAGE, "https://jobs.lever.co/*/apply*"),
    "ashby": (ashby.FORM_PAGE, "https://jobs.ashbyhq.com/*/application*"),
}
BANNED_PERMISSIONS = {
    "<all_urls>", "activeTab", "tabs", "debugger", "webRequest", "cookies", "history", "downloads", "scripting",
    "nativeMessaging", "management",
}  # fmt: skip
BANNED_KEYS = {
    "web_accessible_resources", "externally_connectable", "key", "optional_permissions", "optional_host_permissions",
}  # fmt: skip
BANNED_WORDS = (
    ".submit(", "requestSubmit", "KeyboardEvent", "keydown", "keypress", "keyup", "eval(", "new Function",
    "execCommand", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "console.", "executeScript",
    "postMessage", 'addEventListener("message"', "importScripts", "import(", 'createElement("script")', "dropEffect",
    "DragEvent",
)  # fmt: skip
ONLY_IN_BACKGROUND = ("fetch(", "chrome.storage", "/plugin/", "Authorization")
NEVER_ON_THE_PAGE = ("XMLHttpRequest", "WebSocket", "sendBeacon")
HONEST = (
    "Filling shares these answers and your PDF with the site, even before you press Submit. Other plugins in this "
    "browser can read them too."
)
# Each is the whole rule in one line, so a slip that keeps the rule's words elsewhere still shows here
LINES_THAT_HOLD_A_RULE = (
    (BACKGROUND, "if (record?.ticket && here && record.answered) {"),
    (BACKGROUND, "JOB_PAGE.test(answer.body.job) ? `${DASHBOARD}${answer.body.job}` : undefined"),
    (PAGE, "const why = setLine(line, boxes(root).get(line.key));"),
    (PAGE, 'if (!box.matches(TEXT_BOX)) return "cant-set";'),
    (PAGE, """if (line.kind === "consent") return box.matches('input[type="checkbox"]') ? tick(box) : "cant-set";"""),
)
ON_THE_FORM = 'if (!(await onForm(tabId, record))) return { stop: "report" };'
ELSEWHERE = (
    "No fill was waiting for this tab. If you pressed Open and fill, it may have gone to another tab or browser: check "
    "Settings → Browser plugin."
)
# A string first, so that // in an address isn't taken for a comment
STRING_OR_COMMENT = re.compile(
    r"""("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)|//[^\n]*|/\*.*?\*/""", re.S
)


def without_comments(text):
    return STRING_OR_COMMENT.sub(lambda found: found[1] or "", text)


def code(name):
    """A file's text, without its comments if it's JavaScript."""
    text = (PLUGIN / name).read_text("utf-8")
    return without_comments(text) if name.endswith(".js") else text


def files(*patterns):
    return sorted(str(path.relative_to(PLUGIN)) for pattern in patterns for path in PLUGIN.rglob(pattern))


def page_side():
    """Whatever runs on a job's page: page.js and the boards, once they exist."""
    return [name for name in files("*.js") if name not in (BACKGROUND, POPUP, OPTIONS)]


def function(name):
    """One function of background.js."""
    found = re.search(rf"^(?:async )?function {name}\(.*?^}}$", code(BACKGROUND), re.S | re.M)
    assert found, name
    return found[0]


def handlers(name):
    """The names a background.js `Map` answers to, and the function each runs."""
    found = re.search(rf"^const {name} = new Map\(\[(.*?)\]\);$", code(BACKGROUND), re.S | re.M)
    assert found, name
    return dict(re.findall(r'\["(\w+)", (\w+)\]', found[1]))


@pytest.fixture
def manifest():
    return json.loads((PLUGIN / "manifest.json").read_text("utf-8"))


def test_comments_are_taken_out_and_strings_stay():
    text = 'const a = "http://x"; // eval(\n/* console.\n*/ const b = `//${a}`;'
    assert without_comments(text) == 'const a = "http://x"; \n const b = `//${a}`;'


# The manifest


def test_the_manifest_s_frame(manifest):
    assert (manifest["manifest_version"], manifest["version"]) == (3, "0.1.0")
    assert manifest["minimum_chrome_version"] == "120"
    assert manifest["background"] == {"service_worker": "background.js"}
    assert manifest["action"]["default_popup"] == "popup.html"
    assert manifest["options_ui"] == {"page": "options.html", "open_in_tab": True}


def test_the_plugin_asks_only_for_storage(manifest):
    assert manifest["permissions"] == ["storage"]
    assert not BANNED_PERMISSIONS & set(manifest["permissions"] + manifest["host_permissions"])


def test_the_hosts_are_exactly_these(manifest):
    assert len(manifest["host_permissions"]) == len(HOSTS) and set(manifest["host_permissions"]) == HOSTS


def test_nothing_is_open_to_pages_or_other_plugins_and_no_id_is_fixed(manifest):
    assert not BANNED_KEYS & set(manifest)


def test_the_page_part_runs_only_on_form_pages_in_the_top_frame(manifest):
    scripts = manifest["content_scripts"]
    assert [script["matches"] for script in scripts] == [[pattern] for _, pattern in FORM_PAGES.values()]
    assert [script["js"] for script in scripts] == [[f"boards/{site}.js", PAGE] for site in FORM_PAGES]
    for script in scripts:
        assert set(script) == {"matches", "js", "run_at"} and script["run_at"] == "document_idle"


@pytest.mark.parametrize(("site", "form_page", "pattern"), [(site, *found) for site, found in FORM_PAGES.items()])
def test_each_form_page_is_open_to_the_app_and_reached_by_the_plugin(manifest, site, form_page, pattern):
    address = form_page.format("acme", "123")
    parts = urlsplit(address)
    assert not closed_site(address)
    assert f"{parts.scheme}://{parts.hostname}/*" in manifest["host_permissions"]
    assert fnmatchcase(address, pattern)


# What no file may hold


@pytest.mark.parametrize("word", BANNED_WORDS)
def test_no_file_holds_a_banned_word(word):
    assert [name for name in files("*.js", "*.html") if word in code(name)] == []


@pytest.mark.parametrize("word", ONLY_IN_BACKGROUND)
def test_only_the_background_part_holds(word):
    assert word in code(BACKGROUND)
    assert [name for name in files("*.js", "*.html") if name != BACKGROUND and word in code(name)] == []


@pytest.mark.parametrize("word", NEVER_ON_THE_PAGE)
def test_the_page_side_makes_no_network_call(word):
    assert [name for name in page_side() if word in code(name)] == []


def test_the_only_events_made_are_input_and_change():
    for name in files("*.js"):
        for event, first in re.findall(r"new\s+(\w*Event)\(\s*([^,)\s]*)", code(name)):
            assert (event, first) in (("Event", '"input"'), ("Event", '"change"')), name


def test_a_click_is_made_once_and_never_finds_a_button():
    clicks = [name for name in files("*.js") for _ in re.findall(r"\.click\(", code(name))]
    assert clicks == ([PAGE] if (PLUGIN / PAGE).exists() else [])
    finds = re.compile(r"""(?:querySelector(?:All)?|closest|matches)\(\s*["'`][^"'`]*\bbutton\b""")
    assert [name for name in page_side() if finds.search(code(name))] == []


def test_no_page_has_inline_scripts_or_styles():
    for name in files("*.html"):
        page = code(name)
        assert re.findall(r"<script(?![^>]*\bsrc=)", page) == [] and "<style" not in page, name
        assert re.findall(r"""\s(?:on\w+|style)=["']""", page) == [], name


# The background part


def test_the_dashboard_s_address_is_written_once():
    lines = [line for line in code(BACKGROUND).splitlines() if "127.0.0.1" in line]
    assert lines == ['const DASHBOARD = "http://127.0.0.1:8765";']
    assert [name for name in files("*.js", "*.html") if name != BACKGROUND and "127.0.0.1" in code(name)] == []


def test_form_addresses_come_from_the_manifest_alone():
    assert "MANIFEST = chrome.runtime.getManifest()" in code(BACKGROUND) and "MANIFEST.content_scripts" in code(
        BACKGROUND
    )
    assert [site for site in ("greenhouse", "lever", "ashbyhq") if site in code(BACKGROUND)] == []


def test_each_call_sends_no_cookie_follows_no_redirect_and_keeps_no_copy():
    call = function("call")
    assert 'credentials: "omit"' in call and 'redirect: "error"' in call and 'cache: "no-store"' in call
    assert "protocol: PROTOCOL" in call and code(BACKGROUND).count("fetch(") == 1


def test_the_two_sides_speak_the_same_protocol():
    assert f"const PROTOCOL = {plugin.PROTOCOL};" in code(BACKGROUND)


def test_a_failed_call_keeps_no_error_text():
    assert "catch {" in function("call") and "catch (" not in code(BACKGROUND)


def test_each_part_has_its_own_map_of_handlers():
    assert handlers("PAGE") == {"open": "open", "form": "form", "file": "file", "done": "done"}
    assert handlers("POPUP") == {"status": "status", "fill": "fill"}
    assert handlers("OPTIONS") == {"pair": "pair", "paired": "paired"}


def test_a_message_is_checked_for_who_sent_it_before_any_handler():
    gate = function("handlerFor")
    assert gate.index("sender.id !== chrome.runtime.id") < gate.index("POPUP.get")
    assert "sender.url === POPUP_PAGE" in gate and "sender.url === OPTIONS_PAGE" in gate
    assert "sender.frameId === 0 && sender.tab?.url && FORM_ORIGINS.has(sender.origin)" in gate
    assert code(BACKGROUND).count("onMessage.addListener") == 1


def test_no_other_plugin_or_page_can_reach_the_background_part():
    assert "External" not in code(BACKGROUND)


def test_no_address_ever_comes_from_a_message():
    assert re.findall(r"\bmessage\.(?:address|url|origin|tab)\b", code(BACKGROUND)) == []
    assert "address = sender.tab.url" in function("open")
    assert "address: tab.url" in function("fill") and "address: tab.url" in function("status")
    assert "chrome.tabs.query" in function("activeTab")


def test_only_the_popup_s_press_reaches_the_fill_route():
    calling = re.findall(r'^(?:async )?function (\w+)\((?:(?!^}$).)*?call\("fill"', code(BACKGROUND), re.S | re.M)
    assert calling == ["fill"] and "fill" not in {*handlers("PAGE"), *handlers("OPTIONS")}


def test_the_tab_s_address_is_checked_again_before_the_form_and_a_pdf():
    assert "chrome.tabs.get(tabId)" in function("onForm") and "  return tab.url === record.address;\n" in function(
        "onForm"
    )
    for name, route in (("form", 'call("form"'), ("file", 'call("file"')):
        assert function(name).index(ON_THE_FORM) < function(name).index(route)


def test_a_slot_is_one_the_pack_named():
    assert "!record.slots.includes(message.slot)" in function("file")


def test_a_form_the_app_can_t_read_stays_for_the_page_part_to_report():
    form = function("form")
    refused = form.index("answer.status === 400 || answer.status === 413")
    assert form.index('{ stop: "report" }', refused) < form.index("const problem = answer.status === 200")
    assert '"moved"' not in code(BACKGROUND) and function("onForm")


def test_storage_is_locked_before_the_key_is_saved():
    pair = function("pair")
    assert (
        pair.index("KEY_SHAPE")
        < pair.index("setAccessLevel")
        < pair.index('call("hello"')
        < pair.index("storage.local.set(")
    )
    assert code(BACKGROUND).count("storage.local.set(") == 1
    assert code(BACKGROUND).count("setAccessLevel(TRUSTED)") == 2  # and again at each start


def test_no_reply_holds_the_ticket_or_the_key():
    replies = [re.sub(r'"[^"]*"', "", reply) for reply in re.findall(r"return \{[^\n]*", code(BACKGROUND))]
    assert replies and [reply for reply in replies if re.search(r"\b(?:ticket|key)\b", reply)] == []


# The page part


def page_function(name):
    found = re.search(rf"^(?:async )?function {name}\(.*?^}}$", code(PAGE), re.S | re.M)
    assert found, name
    return found[0]


@pytest.mark.parametrize("name", [PAGE, *(f"boards/{site}.js" for site in FORM_PAGES)])
def test_the_page_part_s_files_exist(name):
    assert (PLUGIN / name).is_file()


@pytest.mark.parametrize("site", FORM_PAGES)
def test_a_board_is_one_constant_with_an_anchor(site):
    assert code(f"boards/{site}.js").startswith("const BOARD = {") and "anchor:" in code(f"boards/{site}.js")


def test_exactly_one_choice_is_picked_by_its_whole_words():
    only = page_function("only")
    assert "tidy(textOf(item)) === wanted" in only and "return found.length === 1 ? found[0] : undefined;" in only


def test_a_stop_is_looked_for_before_each_line_and_after_the_last():
    stop_here = page_function("stopHere")
    assert 'if (location.href !== address || !root.isConnected) return "unexpected-page";' in stop_here
    assert 'return botCheck() ? "bot-check" : undefined;' in stop_here
    fill = page_function("fillLines")
    loop = fill[fill.index("for (const [at, line]") : fill.index("await quiet();")]
    assert loop.index("const stop = stopHere(address, root);") < loop.index("await setOne(line, root)")
    assert "if (stop) return stopAt(stop, lines.slice(at), done, left, root);" in loop
    after = fill[fill.index("await quiet();") :]
    assert after.index("const late = stopHere(address, root);") < after.index("readBack(done, left, root)")
    assert "if (late) return stopAt(late, [], done, left, root);" in after


def test_a_line_counts_as_set_only_if_it_reads_back():
    assert "if (list && same(name ?? line.value, value)) stuck += 1;" in page_function("readBack")


def test_only_a_group_of_choices_may_share_a_key():
    assert re.search(r"^const isGroup = .*\.every\(\(box\) => box\.matches\(TICK_BOX\)\);$", code(PAGE), re.M)
    assert 'if (!list || (list.length > 1 && !isGroup(line, list))) return "no-box";' in page_function("setLine")
    assert (
        'if (list?.length !== 1 || !list[0].matches(\'input[type="file"]\')) return { why: "no-box" };'
        in page_function("upload")
    )


def test_the_form_is_found_once_and_only_a_formless_board_reads_the_whole_page():
    assert code(PAGE).count("querySelector(BOARD.anchor)") == 1
    assert 'anchor?.closest("form") ?? (anchor && BOARD.formless ? document.body : undefined)' in code(PAGE)
    fill = page_function("fillTheForm")
    assert fill.index("await quiet();") < fill.index("const root = rootOf(anchor);")
    assert [name for name in files("boards/*.js") if "formless: true" in code(name)] == ["boards/ashby.js"]
    assert "boxes(root)" in page_function("fillTheForm") and "function boxes(root)" in page_function("boxes")
    assert 'anchor: "#application-form input[name=email]"' in code("boards/lever.js")


def test_a_box_that_isn_t_showing_is_not_read():
    boxes = page_function("boxes")
    assert 'const shown = box.type === "file" || box.checkVisibility(box.matches(DROPDOWN) ? CSS_ONLY : SEEN);' in boxes
    assert "const CSS_ONLY = { checkVisibilityCSS: true };" in code(PAGE)
    assert "if (key && shown && !SKIPPED.has(box.type))" in boxes
    assert "const SEEN = { checkOpacity: true, checkVisibilityCSS: true };" in code(PAGE)


def test_a_value_cut_in_half_is_still_well_formed():
    assert ".slice(0, MAX_VALUE).toWellFormed()" in code(PAGE)


def test_the_click_is_inside_tick_after_its_check():
    tick = page_function("tick")
    assert tick.index("box.matches(TICK_BOX) && !box.checked") < tick.index(".click(")
    assert """TICK_BOX = 'input[type="radio"], input[type="checkbox"]';""" in code(PAGE)


def test_only_raise_makes_an_event():
    raise_ = page_function("raise")
    assert raise_.count("dispatchEvent") == 2 and code(PAGE).count("dispatchEvent") == 2
    assert '"input", { bubbles: true }' in raise_ and '"change", { bubbles: true }' in raise_


def test_the_skip_list_names_password_and_the_other_sorts():
    skipped = re.search(r"^const SKIPPED = new Set\(\[(.*?)\]\);$", code(PAGE), re.M)[1]
    assert set(re.findall(r'"(\w+)"', skipped)) == {"hidden", "password", "submit", "button", "reset", "image"}


def test_no_message_from_the_page_carries_an_address():
    messages = re.findall(r"\bask\(\{[^}]*\}", code(PAGE))
    assert len(messages) == 4 and code(PAGE).count("sendMessage(") == 1  # open, file, done, form
    assert [message for message in messages if re.search(r"address|location|href", message)] == []


def test_the_page_part_never_looks_outside_its_own_frame():
    assert [word for word in ("window.top", "parent.", "contentWindow", "contentDocument") if word in code(PAGE)] == []


def test_tidy_takes_out_the_same_characters_as_the_app():
    assert f"const CONTROL = /{CONTROL.pattern}/g;" in code(PAGE)


def test_every_why_and_stop_is_one_the_app_knows():
    literals = set(re.findall(r'"([^"\n]*)"', code(PAGE)))
    known = {*fills.LEFT, *plugin.PAGE_STOPS}
    assert known <= literals
    assert {word for word in literals if re.fullmatch(r"[a-z]+(?:-[a-z]+)+", word)} <= known | {
        "aria-label", "aria-required", "one-time-code",
    }  # fmt: skip


def test_the_page_s_words_are_read_only_for_the_limit_check():
    assert code(PAGE).count("innerText") == 1
    assert "innerText" in re.search(r"^const whyNoForm = .*$", code(PAGE), re.M)[0]


def test_begin_is_answered_at_once_and_only_from_this_plugin():
    listener = code(PAGE).partition("onMessage.addListener(")[2]
    assert "async" not in listener.partition("\n")[0] and "reply({});" in listener
    assert 'if (sender.id === chrome.runtime.id && message?.do === "begin") run();' in listener


@pytest.mark.parametrize(("name", "line"), LINES_THAT_HOLD_A_RULE)
def test_a_line_that_holds_a_rule_is_still_there(name, line):
    assert line in code(name)


# The popup and the options page


def test_the_popup_says_where_a_fill_may_have_gone_and_what_filling_shares():
    popup = code(POPUP)
    assert ELSEWHERE in popup and HONEST in popup
    assert (
        "This tab was reloaded, so the form may be empty: a reload doesn't fill again. Press Fill to start over."
        in popup
    )
    assert (
        "Log in to the dashboard first. If a fill was waiting here, reload this tab afterwards and it goes on." in popup
    )
    assert "Reload this tab: the fill starts by itself." in popup
    assert "the plugin stops wherever this check shows" in popup
    assert "textContent" in popup


def test_the_popup_has_no_words_for_what_the_app_never_sends_it():
    assert "too-many-fields" not in code(POPUP)
    assert "This page isn't the form of one of your jobs." in code(POPUP)


def test_the_options_page_s_failure_words_say_nothing_new_was_kept():
    assert "nothing was kept" not in code(OPTIONS) and code(OPTIONS).count("nothing new was kept") == 2


def test_the_options_page_says_where_the_plugin_talks_and_keeps_no_key():
    assert "port 8765" in code("options.html") and 'type="password"' in code("options.html")
    assert "chrome.runtime.sendMessage" in code(OPTIONS)
