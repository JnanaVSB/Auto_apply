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
from app.submit import ashby, fill_words, greenhouse, lever
from app.web import plugin

PLUGIN = Path(__file__).parents[1] / "plugin"
BACKGROUND, PAGE, WATCH, POPUP, OPTIONS = "background.js", "page.js", "watch.js", "popup.js", "options.js"
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
    """Whatever runs on a job's page: page.js, watch.js and the boards, once they exist."""
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
    assert [script["js"] for script in scripts] == [[f"boards/{site}.js", WATCH, PAGE] for site in FORM_PAGES]
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
    assert handlers("PAGE") == {
        "open": "open", "form": "form", "file": "file", "done": "done", "moved": "moved", "watch": "watch",
        "read": "read", "thanked": "thanked",
    }  # fmt: skip
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
    assert 'stop: "moved"' not in code(BACKGROUND)


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


def page_function(name, file=PAGE):
    found = re.search(rf"^(?:async )?function {name}\(.*?^}}$", code(file), re.S | re.M)
    assert found, name
    return found[0]


@pytest.mark.parametrize("name", [PAGE, WATCH, *(f"boards/{site}.js" for site in FORM_PAGES)])
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
    messages = re.findall(r"\bask\(\{[^}]*\}", code(PAGE) + code(WATCH))
    assert len(messages) == 9  # moved, open, form, file, done, watch, thanked, and a read of each sort
    assert code(PAGE).count("sendMessage(") == 1 and "sendMessage(" not in code(WATCH)
    assert [message for message in messages if re.search(r"address|location|href", message)] == []


def test_the_page_part_never_looks_outside_its_own_frame():
    words = ("window.top", "parent.", "contentWindow", "contentDocument")
    assert [(name, word) for name in page_side() for word in words if word in code(name)] == []


def test_tidy_takes_out_the_same_characters_as_the_app():
    assert f"const CONTROL = /{CONTROL.pattern}/g;" in code(PAGE)


def test_every_why_and_stop_is_one_the_app_knows():
    literals = set(re.findall(r'"([^"\n]*)"', code(PAGE)))
    known = {*fill_words.LEFT, *plugin.PAGE_STOPS}
    assert known <= literals
    assert {word for word in literals if re.fullmatch(r"[a-z]+(?:-[a-z]+)+", word)} <= known | {
        "aria-label", "aria-required", "one-time-code",
    }  # fmt: skip


def test_a_page_must_be_where_it_landed_before_anything_is_asked():
    assert 'const landedAt = () => performance.getEntriesByType("navigation")[0]?.name;' in code(PAGE)
    first, second, third = page_function("fillTheForm").splitlines()[1:4]
    assert first == "  const address = landedAt();"
    assert second == '  if (location.href !== address) return ask({ do: "moved" });'
    assert 'ask({ do: "open" })' in third and page_function("fillTheForm").count("location.href") == 1
    assert "call(" not in function("moved")


def test_the_page_s_words_are_read_only_for_the_limit_check():
    assert code(PAGE).count("innerText") == 1
    assert "innerText" in re.search(r"^const whyNoForm = .*$", code(PAGE), re.M)[0]


def test_begin_is_answered_at_once_and_only_from_this_plugin():
    listener = code(PAGE).partition("onMessage.addListener(")[2]
    assert "async" not in listener.partition("\n")[0] and "reply({ moved: location.href !== landedAt() });" in listener
    assert 'if (sender.id === chrome.runtime.id && message?.do === "begin") run();' in listener


@pytest.mark.parametrize(("name", "line"), LINES_THAT_HOLD_A_RULE)
def test_a_line_that_holds_a_rule_is_still_there(name, line):
    assert line in code(name)


# The popup and the options page


# The watcher


def top_level_names(name):
    return set(re.findall(r"^(?:const|let|function|async function) (\w+)", code(name), re.M))


def listener(event):
    """A chrome.tabs listener's whole body, which must start at the top level or a sleeping worker never hears it."""
    found = re.search(rf"^chrome\.tabs\.{event}\.addListener\(.*?^\}}\);$", code(BACKGROUND), re.S | re.M)
    assert found, event
    return found[0]


@pytest.mark.parametrize(
    "word", [".click(", "dispatchEvent", "preventDefault", "stopPropagation", "stopImmediatePropagation", "submit("]
)
def test_the_watcher_can_t_press_block_or_delay_anything(word):
    assert word not in code(WATCH)


def test_the_watcher_says_so_at_its_top():
    assert "presses, blocks and delays nothing" in (PLUGIN / WATCH).read_text("utf-8").splitlines()[1]


def test_the_watcher_only_defines_things_as_it_loads_before_page_js():
    lines = [line for line in code(WATCH).splitlines() if line and not line.startswith((" ", "}", ")"))]
    assert lines and [line for line in lines if not re.match(r"(?:const|let|function|async function) ", line)] == []
    assert "new Set(" in code(WATCH) and not re.search(r'^const \w+ = (?!\(|/|"|new Set\(|\d)', code(WATCH), re.M)


@pytest.mark.parametrize("other", [PAGE, *(f"boards/{site}.js" for site in FORM_PAGES)])
def test_no_top_level_name_is_shared_with_another_page_file(other):
    assert not top_level_names(WATCH) & top_level_names(other)  # they share one scope: a repeat would stop the file


def test_page_js_starts_the_watcher_once_after_the_fill_and_it_can_t_report_a_stop():
    run = page_function("run")
    assert code(PAGE).count("watch()") == 1 and "await watch().catch(() => {});" in run
    assert run.index("finally") < run.index("watch()")
    assert run.splitlines()[-2] == "  await watch().catch(() => {});"


def test_only_a_trusted_click_counts_and_that_is_looked_at_first():
    assert [name for name in files("*.js") if "isTrusted" in code(name)] == [WATCH]
    assert page_function("heard", WATCH).splitlines()[1] == "  if (!event.isTrusted) return;"


def test_one_click_listener_in_the_capture_phase():
    assert [name for name in page_side() if 'addEventListener("click"' in code(name)] == [WATCH]
    assert code(WATCH).count('addEventListener("click"') == 1
    assert 'document.addEventListener("click", heard, true);' in page_function("watch", WATCH)


def test_a_press_is_read_at_once_and_a_lost_message_is_quiet():
    heard = page_function("heard", WATCH)
    assert "await" not in heard and "async" not in heard
    assert 'ask({ do: "read", state: "clicked", values: readForm(formRoot) });' in heard
    assert heard.splitlines()[-2].endswith(".catch(() => {});")


def test_a_press_must_be_on_a_control_you_can_see_that_holds_no_box():
    heard = page_function("heard", WATCH)
    assert (
        heard.index("event.isTrusted") < heard.index("control.checkVisibility(SEEN)") < heard.index('ask({ do: "read"')
    )
    assert "control.querySelector(HOLDS_A_BOX)" in heard
    assert 'const HOLDS_A_BOX = "input, select, textarea, label";' in code(WATCH)


def test_a_press_is_only_a_board_s_named_submit_or_a_real_submit_of_the_form():
    heard = page_function("heard", WATCH)
    assert "event.composedPath().find(isSubmit)" in heard and "formRoot?.contains(control)" in heard
    submit = page_function("isSubmit", WATCH)
    assert submit.index("BOARD.submit") < submit.index("getAttribute")
    assert "return node.matches(BOARD.submit);" in submit
    assert '["INPUT", "BUTTON"].includes(node.tagName)' in submit
    assert 'node.getAttribute("type")?.toLowerCase() === "submit"' in submit  # a <button> with no type doesn't say so
    assert "node.form === formRoot" in submit


def test_no_words_decide_a_press():
    for word in ("SEND_WORDS", "wordsOf", "saysSubmit", "isControl", "textContent, control.value", "aria-label"):
        assert word not in code(WATCH)


def test_no_board_names_its_submit_until_a_real_form_has_shown_it():
    assert [name for name in files("boards/*.js") if re.search(r"\bsubmit\b", code(name))] == []
    assert "No Submit is named here" in (PLUGIN / "boards/ashby.js").read_text("utf-8")


def test_a_read_goes_through_boxes_and_skips_what_it_must():
    read = page_function("readForm", WATCH)
    assert "boxes(root)" in read and "isSecret(box) || box.matches(DROPDOWN)" in read
    assert "box.autocomplete.split(/\\s+/).some((word) => SECRET_BOXES.has(word))" in code(
        WATCH
    )  # it can hold a section
    assert "|| SECRET_QUESTION.test(question(box));" in code(WATCH)
    assert "const SECRET_QUESTION = /password/i;" in code(WATCH)
    here = page_function("shownHere", WATCH)
    assert 'if (list[0].tagName === "SELECT" && !list[0].value) return "";' in here
    assert 'shownValue({ key, kind, value: "ticked" }, list)' in here
    assert 'ownLabel(box).trim() || box.value || "ticked"' in here  # a ticked box with no words is still ticked
    assert "cut(shownHere(key, list))" in read
    assert "shownHere" not in code(PAGE)  # the fill's own read-back keeps what shownValue says
    assert "list.some(" in read  # one such box in a group is enough
    assert read.index("continue") < read.index("shownHere")
    assert set(re.findall(r'"([\w-]+)"', re.search(r"^const SECRET_BOXES = .*$", code(WATCH), re.M)[0])) == {
        "current-password", "new-password", "one-time-code",
    }  # fmt: skip
    assert code(WATCH).count("querySelector") == 2 and "innerText" not in code(WATCH)


def test_one_watcher_at_a_time_and_only_on_the_page_it_landed_on():
    watch = page_function("watch", WATCH)
    assert watch.index("lastKept = undefined;") < watch.index("if (watching) return;")
    assert watch.index("if (watching) return;") < watch.index("watching = true;") < watch.index("await")
    assert watch.index("location.href !== landedAt()") < watch.index('ask({ do: "watch" })')
    assert 'const told = await ask({ do: "watch" });' in watch and "if (!told?.on) return;" in watch
    assert "finally" in watch and "watching = false;" in watch
    assert watch.index("await quiet();") < watch.index("formRoot = rootOf(anchored());")
    assert "while (await look()) await wait(REREAD_MS);" in watch and "const REREAD_MS = 5000;" in code(WATCH)


def test_a_look_stops_where_it_must_and_sends_only_what_changed():
    look = page_function("look", WATCH)
    assert look.splitlines()[1] == "  if (location.href !== landedAt()) return false;"
    assert look.index("isConnected") < look.index("readForm(formRoot)")
    assert "if (!formRoot) return true;" in look
    assert look.index("=== lastKept") < look.index('ask({ do: "read", state: "reread", values })')
    assert "if (reply?.stop) return false;" in look and "if (reply?.kept) lastKept = sent;" in look


def test_the_background_starts_a_watcher_only_for_a_fill_that_is_out():
    watch = function("watch")
    assert "record?.ticket" in watch and "record.answered" in watch and "await onForm(tabId, record)" in watch
    assert "call(" not in watch


def test_a_read_is_checked_before_it_is_sent():
    read = function("sendRead")
    first = read.index("call(")
    for check in ("!record?.ticket", 'READ_STATES.has(state)', "Array.isArray(values)", "sender.url === record.address",
                  "await onForm(tabId, record)"):  # fmt: skip
        assert read.index(check) < first, check
    assert read.count("{ stop: true }") == 4
    assert "if (latest?.ticket !== record.ticket) return { stop: true };" in read
    assert re.search(r'^const READ_STATES = new Set\(\["reread", "clicked"\]\);$', code(BACKGROUND), re.M)
    assert 'state === "clicked" ? sender.url === record.address : await onForm(tabId, record)' in read


def test_what_a_read_keeps_of_how_it_went():
    read = function("sendRead")
    assert "NO_FILL.has(answer.body.error)" in read
    assert re.search(r'^const NO_FILL = new Set\(\["no-ticket", "no-key", "wrong-plugin"\]\);$', code(BACKGROUND), re.M)
    assert "{ ticket: record.ticket, state, values, left: [] }" in read
    assert "unsaved: true" in read and "unsaved: false" in read and "looks: true" in read and "{ kept: true }" in read
    assert read.index("await call(") < read.index("await recordOf(tabId);", read.index("await call("))


def test_a_press_on_its_way_is_held_from_before_anything_is_awaited():
    read = function("read")
    assert "await" not in read and "async" not in read
    assert read.index("sendRead(message, sender)") < read.index("pressing.set(tabId, ended);")
    assert 'if (message.state !== "clicked") return going;' in read  # a reread holds nothing up
    assert "going.catch(() => {}).finally(" in read  # a failed press can't stop `gone`
    assert "if (pressing.get(tabId) === ended) pressing.delete(tabId);" in read


@pytest.mark.parametrize(("event", "after"), [("onRemoved", "leftForm(tabId)"), ("onUpdated", "recordOf(tabId)")])
def test_a_leaving_tab_waits_for_a_press_on_its_way_first(event, after):
    body = listener(event)
    assert body.index("await pressing.get(tabId);") < body.index(after)


def test_a_tab_that_left_says_gone_and_nothing_else():
    assert '{ ticket: record.ticket, state: "gone", values: [], left: [] }' in function("leftForm")
    assert "record?.ticket" in function("leftForm")


def test_closing_the_tab_says_so_and_forgets_its_record():
    removed = listener("onRemoved")
    assert removed.index("leftForm(tabId)") < removed.index("chrome.storage.session.remove(recordName(tabId))")


def test_leaving_the_address_says_so_and_a_reload_doesn_t():
    updated = listener("onUpdated")
    assert "if (!change.url && !change.status) return;" in updated
    assert "if (!record?.ticket || tab.url === record.address) return;" in updated


def test_leaving_never_wipes_a_ticket_the_tab_took_meanwhile():
    updated = listener("onUpdated")
    left = updated.index("await leftForm(tabId);")
    assert left < updated.index("const latest = await recordOf(tabId);") < updated.index("keep(tabId,")
    assert "if (latest?.ticket === record.ticket) await keep(tabId, noTicket(latest));" in updated


# The thank-you sign


def test_no_board_file_of_the_plugin_holds_a_sign():
    assert [name for name in files("boards/*.js") if "thanks" in code(name).lower()] == []


def test_the_only_sign_is_the_one_the_app_sent():
    assert "const thanks = answer.body.thanks ?? null;" in function("open")
    assert code(BACKGROUND).count("answer.body.thanks") == 1
    assert "thanksSign = told.thanks;" in page_function("watch", WATCH)
    assert code(WATCH).count("thanksSign =") == 1


def test_the_page_says_only_that_it_saw_the_sign():
    assert 'ask({ do: "thanked" })' in page_function("thanksAfter", WATCH)
    assert "message." not in function("thanked")


def test_the_background_checks_before_it_says_thanked():
    thanked = function("thanked")
    first = thanked.index("saidThanks(")
    for check in ("!record?.ticket", "!record.thanks?.selector", "!record.clickedAt", "await onForm(tabId, record)"):
        assert thanked.index(check) < first, check


def test_a_sign_is_said_with_nothing_read_and_believed_only_if_recorded():
    said = function("saidThanks")
    assert '{ ticket: record.ticket, state: "thanked", values: [], left: [] }' in said
    assert "if (answer.body.recorded !== true) return;" in said
    assert said.index("answer.body.recorded") < said.index('flag(tabId, "OK")')
    assert said.index("await recordOf(tabId);") < said.index("recorded: true")


def test_a_press_is_timed_only_when_the_app_took_it():
    read = function("sendRead")
    assert 'state === "clicked" ? { clickedAt: Date.now() } : {}' in read
    assert read.index("if (answer.status !== 200)") < read.index("clickedAt")


def test_the_page_is_given_only_a_selector_sign():
    watch = function("watch")
    assert "const thanks = on && record.thanks?.selector ? record.thanks : undefined;" in watch
    assert "return { on, thanks };" in watch


def test_an_address_sign_needs_a_press_and_the_same_origin_and_comes_before_gone():
    at = function("atThanksPage")
    assert "record.thanks?.path" in at and "record.clickedAt" in at
    assert "originOf(tab.url) === originOf(record.address)" in at
    assert "new URL(tab.url).pathname.endsWith(record.thanks.path)" in at
    updated = listener("onUpdated")
    assert updated.index("atThanksPage(record, tab)") < updated.index("saidThanks(") < updated.index("leftForm(tabId)")


def test_one_look_for_a_sign_at_a_time_for_a_minute_and_a_bad_one_is_quiet():
    after = page_function("thanksAfter", WATCH)
    assert "if (!reply?.kept || !thanksSign || lookingForThanks) return;" in after
    assert "const THANKS_LOOKS = 60;" in code(WATCH) and "const THANKS_MS = 1000;" in code(WATCH)
    assert "finally" in after and "lookingForThanks = false;" in after
    assert "catch {\n    return true;\n  }" in page_function("shownBeforePress", WATCH)  # a bad selector never looks


def test_a_sign_already_showing_at_the_press_is_never_looked_for():
    heard = page_function("heard", WATCH)
    trusted, sent = heard.index("event.isTrusted"), heard.index('ask({ do: "read"')
    before, looks = heard.index("const already = shownBeforePress();"), heard.index("thanksAfter(reply)")
    assert trusted < sent < before < looks
    assert "(already ? undefined : thanksAfter(reply))" in heard
    assert code(WATCH).count("thanksAfter(") == 2  # its own line, and here


def test_the_sign_is_read_by_the_page_s_own_tidy():
    assert "tidy(element.textContent).includes(tidy(thanksSign.words))" in code(WATCH)


# Reading a form


def test_a_ticket_may_only_be_for_a_fill_or_a_read():
    assert re.search(r'^const MODES = new Set\(\["fill", "read"\]\);$', code(BACKGROUND), re.M)
    assert 'MODES.has(answer.body.mode) && typeof answer.body.ticket === "string"' in function("open")
    assert "{ ticket, mode, address, slots: [], answered: false, thanks, said: {} }" in function("open")


def test_a_read_form_ends_the_ticket_here_and_sends_back_no_pack():
    form = function("form")
    assert form.index('reason === "read"') < form.index("answer.status === 400")
    assert 'await keep(tabId, { address: record.address, said: { problem: "read" } });' in form
    branch = form[form.index('reason === "read"') : form.index("answer.status === 400")]
    assert 'flag(tabId, "OK")' in branch and 'return { stop: "known" };' in branch and "pack" not in branch


def test_page_js_doesn_t_know_a_read_from_a_fill():
    assert "mode" not in code(PAGE) and '"read"' not in page_function("fillTheForm")


def test_the_popup_says_a_read_in_plain_words():
    assert 'record?.mode === "read" ? "reading" : "filling"' in function("status")
    assert "Reading this form…" in code(POPUP)
    assert "The form was read. Go back to the job's box and reload it." in code(POPUP)


# The guide


def guide():
    return " ".join((PLUGIN.parent / "docs" / "submitting.md").read_text("utf-8").split())


def test_the_guide_opens_with_what_to_read_before_a_first_fill():
    text = guide()
    opening, _, rest = text.partition("## Read this before your first fill")
    first, _, _ = rest.partition("## ")
    assert rest and "##" not in opening
    for number, words in enumerate(
        (
            "Fill only jobs you would apply to",
            "close other tabs on that same site",
            "Load a copy of the plugin",
            "a Chrome profile that holds only this plugin",
            "The key sits unencrypted",
            "Fixes don't arrive by themselves",
        ),
        start=1,
    ):
        assert f"{number}. **" in first and words in first


@pytest.mark.parametrize("word", ["Fill in browser", ".claude", "Claude", " AI "])
def test_the_guide_is_for_the_public(word):
    assert word not in guide()


def test_the_guide_says_what_a_page_can_do():
    text = guide()
    assert "a script on the page can change them: read them before you fill" in text
    assert "each answer waits for you to press Use" in text
    assert "It looks sent. If the site took it, press I submitted it." in text
    assert "a page can also make a click of yours count as that press" in text
    assert "On Ashby nothing is noticed yet" in text
    assert "the saved form stays" in text
    assert "Use works for a file too" in text
    assert "While it watches the form, it never reads a password box" in text
    assert "Reload this page to see how it went" in text
    assert "if its name differs" in text


def test_a_read_of_too_few_questions_says_so():
    assert "This read found too few of the form's questions, so the saved form stays." in code(POPUP)
    assert '["too-few", ' in code(POPUP)


def test_a_press_s_words_go_wherever_a_ticket_goes():
    assert "const { looks, ...rest } = said ?? {};" in code(BACKGROUND)
    assert "return { address, said: rest };" in function("noTicket")
    for found in (function("sendRead"), function("fill"), listener("onUpdated")):
        assert "noTicket(" in found
    assert not re.search(r"keep\(\w+(?:\.id)?, \{ address: \w+\.address, said: \w+\.said \}\)", code(BACKGROUND))


@pytest.mark.parametrize("name", ["form", "done"])
def test_a_save_after_a_call_looks_at_the_record_again(name):
    found = function(name)
    assert found.index('call("') < found.index("const latest = await recordOf(tabId);")
    assert "if (latest?.ticket !== record.ticket) return" in found


def test_the_popup_says_it_looks_sent_and_that_a_read_was_lost():
    popup = code(POPUP)
    assert "It looks sent" not in popup and "You pressed Submit here: say what happened in the job's box." in popup
    assert "The latest read of this form couldn't be saved." in popup and "last good one" not in popup
    assert "Check the form before you press Submit yourself." in popup
    assert "said.looks" in popup and "said.unsaved" in popup
    assert "The site showed its thank-you page after your press of Submit, so this was recorded." in popup
    assert 'said.recorded ? words("recorded") : said.looks && words("looks-sent")' in popup


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
