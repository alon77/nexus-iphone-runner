"""harness — the only surface an iPhone spec touches (`guide iphone:spec_format`). A spec is a Python file at
experts/<expert>/tests/iphone/<name>.py whose top-level code calls these names, injected by serve:

    open(path)            Safari goes to the target host + path (or a full URL)
    tap(css)              closes any Safari tip bubble (it swallows the next tap), then a real native tap on the
                          element (nativeWebTap), so focus raises the keyboard; a tap that leaves a focusable
                          element unfocused is retried once and listed in results.json tap_retries
    type(css, text)       tap, then type the whole text into the focused field in one XCTest typeText (WDA /wda/keys)
    keyboard_up()         True while a native XCUIElementTypeKeyboard is visible
    dismiss_keyboard()    hide the keyboard, wait until it is gone
    rect(css)             getBoundingClientRect as a dict (None when the element is missing)
    viewport()            visual_height, visual_offset_top, inner_height, inner_width, scroll_y
    shot(name)            full-screen simulator screenshot, keyboard included
    js(script)            run JavaScript in the page, `return` hands a value back (document.cookie, input values)
    swipe(css, from_share, to_share)   a real horizontal finger drag at the vertical middle of the element's visible
                          part, from/to a share of the screen width (0 = left edge, 1 = right edge) — 0 to 0.7 is
                          the left-edge swipe iPhone Safari turns into back. The finger is runner/finger.m, the
                          Simulator's own touch builder; a drag starting within 20pt of a screen edge carries that
                          edge's flag, as the iPhone digitizer marks it. XCTest's synthesized drags never trigger
                          Safari's back gesture
    safari_bottom_bar()   where Safari's own bottom bar starts, in the page's CSS px (same axis as rect()):
                          {top, web_view_bottom, screen_bottom}, None when Safari shows no bottom bar; read off the
                          native tree, mapped through a fixed probe element of known CSS height
    expect(name, condition, detail)   one red/green line in results.json

Appium never leaks into a spec. Once per session, before any spec, keyboard_preflight proves the software
keyboard comes up; after the preflight and after each spec's result goes back, clean_state wipes the site's cookies
and storage, so every spec starts clean and the wipe is never inside a timed run.
"""

import os
import subprocess
import time
import xml.etree.ElementTree as ElementTree
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

KEYBOARD_CAPABILITIES = {"appium:connectHardwareKeyboard": False,
                         "appium:forceSimulatorSoftwareKeyboardPresence": True}
APPIUM_URL = "http://127.0.0.1:4723"
WDA_LAUNCH_TIMEOUT_MS = 600_000
DRIVER_NEVER_TIMES_OUT_S = 0
WEBVIEW_CONNECT_TIMEOUT_MS = 60_000
WEBVIEW_ATOM_WAIT_MS = 8_000
NATIVE_IDLE_WAIT_S = 0
NO_SYSLOG_PREDICATE = 'process == "nexus_no_such_process"'
MIN_KEYBOARD_SHRINK_PX = 200
KEYBOARD_WAIT_S = 8
COLD_SAFARI_READY_WAIT_S = 180
OPEN_WAIT_S = 15
APPIUM_PAGE_LOAD_WAIT_S = 1
SAFARI_SETTLED_S = 2.0
TYPING_KEYS_PER_MINUTE = 600
OSASCRIPT_TIMEOUT_S = 30
POLL_S = 0.25
NATIVE_CONTEXT = "NATIVE_APP"
CSS_SELECTOR = "css selector"
IOS_CLASS_CHAIN = "-ios class chain"
CLASS_NAME = "class name"
KEYBOARD_CLASS = "XCUIElementTypeKeyboard"
KEYBOARD_TYPE_COMMAND = "wda_keys"
KEYBOARD_TYPE_ROUTE = ("POST", "/session/$sessionId/keys")
CLOSE_BUTTONS_CHAIN = ('**/XCUIElementTypeButton[`(name IN {"Close", "Not Now"} OR label IN {"Close", "Not Now"})'
                       ' AND name != "StopButton"`]')
GONE_ELEMENT_ERROR = "StaleElementReferenceException"
WEB_CONTENT_BUTTONS_CHAIN = "**/XCUIElementTypeWebView/**/XCUIElementTypeButton"
PROBE_INPUT_ID = "nexus_keyboard_probe"
KEYBOARD_NOT_SHOWING = "software keyboard not showing"
SPEC_VERBS = ("open", "tap", "type", "keyboard_up", "dismiss_keyboard", "rect", "viewport", "shot", "js", "swipe",
              "safari_bottom_bar")
WEB_VIEW_CLASS = "XCUIElementTypeWebView"
SIMULATOR_UDID_ENV = "UDID"
FINGER_TIMEOUT_S = 10
SAFARI_EDGE_ZONE_PT = 20
SWIPE_SETTLE_S = 1.0
ACCESSIBILITY_ID = "accessibility id"
BAR_PROBE_ID = "nexus_bar_probe"
BAR_PROBE_CSS_HEIGHT = 100
BAR_ZONE_FROM_SHARE = 0.5
BAR_MAX_HEIGHT_SHARE = 0.25
NOT_A_BAR_CLASSES = (WEB_VIEW_CLASS, KEYBOARD_CLASS)
BAR_PROBE_JS = ("const probe = document.createElement('button'); probe.id = arguments[0];"
                " probe.setAttribute('aria-label', arguments[0]);"
                " probe.style.cssText = 'position:fixed;top:0;left:0;width:100px;height:' + arguments[1] + 'px;"
                "margin:0;padding:0;border:0;background:transparent;z-index:2147483647';"
                " document.body.appendChild(probe); return probe.getBoundingClientRect().top;")
VIEWPORT_JS = ("return {visual_height: window.visualViewport.height, visual_offset_top: window.visualViewport.offsetTop,"
               " inner_height: window.innerHeight, inner_width: window.innerWidth, scroll_y: window.scrollY};")
RECT_JS = ("const element = document.querySelector(arguments[0]); if (!element) return null;"
           " const r = element.getBoundingClientRect();"
           " return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height};")
PROBE_INPUT_JS = ("const probe = document.createElement('input'); probe.id = arguments[0];"
                  " probe.style.cssText = 'position:fixed;top:60px;left:16px;width:240px;height:44px;"
                  "font-size:16px;z-index:2147483647'; document.body.appendChild(probe);")
REMOVE_PROBE_JS = "const probe = document.getElementById(arguments[0]); if (probe) probe.remove();"
FOCUS_STATE_JS = ("const element = document.querySelector(arguments[0]);"
                  " if (!element) return {focusable: false, focused: false};"
                  " return {focusable: element.matches('input, textarea, select, [contenteditable]:not([contenteditable=\\'false\\'])'),"
                  " focused: document.activeElement === element};")
BLUR_JS = "if (document.activeElement) document.activeElement.blur();"
LEAVE_PAGE_JS = "window.nexus_left_page = true;"
PAGE_READY_JS = "return window.nexus_left_page ? 'the old page' : document.readyState;"
READY_STATES = ("interactive", "complete")
PROBE_PRESENT_JS = "return !!document.getElementById(arguments[0]);"
CLEAN_PATH = "/robots.txt"
BLANK_PAGE = "about:blank"
CLEAR_STORAGE_JS = "try { localStorage.clear(); sessionStorage.clear(); } catch (error) {}"
HARDWARE_KEYBOARD_OFF_SCRIPT = '''
tell application "Simulator" to activate
tell application "System Events" to tell process "Simulator"
  set hardware_keyboard to menu item "Connect Hardware Keyboard" of menu 1 of menu item "Keyboard" of menu 1 of menu bar item "I/O" of menu bar 1
  if (value of attribute "AXMenuItemMarkChar" of hardware_keyboard) is not missing value then click hardware_keyboard
end tell
'''


def capabilities(udid: str, start_url: str) -> dict:  # kwargs-lint: ignore: device then page, order is canonical
    return {"platformName": "iOS", "appium:automationName": "XCUITest", "browserName": "Safari",
            "appium:udid": udid, "appium:nativeWebTap": True, "appium:showSafariConsoleLog": True,
            "appium:safariInitialUrl": start_url, "appium:wdaLaunchTimeout": WDA_LAUNCH_TIMEOUT_MS,
            "appium:newCommandTimeout": DRIVER_NEVER_TIMES_OUT_S, "appium:showXcodeLog": True,
            "appium:webviewConnectTimeout": WEBVIEW_CONNECT_TIMEOUT_MS, "appium:webviewAtomWaitTimeout": WEBVIEW_ATOM_WAIT_MS,
            "appium:waitForIdleTimeout": NATIVE_IDLE_WAIT_S, "appium:iosSimulatorLogsPredicate": NO_SYSLOG_PREDICATE,
            "appium:maxTypingFrequency": TYPING_KEYS_PER_MINUTE, "pageLoadStrategy": "eager", **KEYBOARD_CAPABILITIES}


def prebuilt_wda_capabilities(app_path: str) -> dict:
    return {"appium:usePreinstalledWDA": True, "appium:prebuiltWDAPath": app_path}


def with_keyboard_typing(driver):
    driver.command_executor.add_command(KEYBOARD_TYPE_COMMAND, *KEYBOARD_TYPE_ROUTE)
    return driver


def with_own_page_wait(driver):
    driver.set_page_load_timeout(APPIUM_PAGE_LOAD_WAIT_S)
    return driver


def bottom_bar_top_pt(native_xml: str, *, screen_height: float) -> Optional[float]:
    tops = []

    def walk(node):
        if node.get("type") in NOT_A_BAR_CLASSES:
            return
        y, height = float(node.get("y", -1)), float(node.get("height", 0))
        low_and_slim = y >= screen_height * BAR_ZONE_FROM_SHARE and 0 < height <= screen_height * BAR_MAX_HEIGHT_SHARE
        if node.get("visible") == "true" and low_and_slim:
            tops.append(y)
        for child in node:
            walk(child)

    walk(ElementTree.fromstring(native_xml))
    return min(tops) if tops else None


def css_y(native_y: float, probe: dict) -> float:
    css_per_point = probe["css_height"] / probe["native_height"]
    return probe["css_top"] + (native_y - probe["native_y"]) * css_per_point


@dataclass
class Expectation:
    name: str
    condition: object
    detail: object = ""


@dataclass
class Recorder:
    out_dir: Path
    spec: Optional[str] = None
    preflight: Optional[dict] = None
    expects: list = field(default_factory=list)
    shots: list = field(default_factory=list)
    tap_retries: list = field(default_factory=list)

    def record(self, expectation: Expectation) -> bool:
        passed = bool(expectation.condition)
        self.expects.append({"name": expectation.name, "ok": passed, "detail": str(expectation.detail)})
        return passed


@dataclass
class SwipeLine:
    start_x: float
    end_x: float
    y: float
    screen: dict

    def edge(self) -> str:
        if self.start_x <= SAFARI_EDGE_ZONE_PT:
            return "left"
        if self.start_x >= self.screen["width"] - SAFARI_EDGE_ZONE_PT:
            return "right"
        return "none"

    def screen_shares(self) -> list:
        width, height = self.screen["width"], self.screen["height"]
        return [f"{self.start_x / width:.4f}", f"{self.y / height:.4f}", f"{self.end_x / width:.4f}", f"{self.y / height:.4f}"]


@dataclass
class RunContext:
    driver: object
    base_url: str
    recorder: Recorder


def _close_unless_gone(button):
    try:
        if button.is_displayed():
            button.click()
    except Exception as error:
        if type(error).__name__ != GONE_ELEMENT_ERROR:
            raise


class Phone:
    def __init__(self, context: RunContext):
        self.driver = context.driver
        self.base_url = context.base_url
        self.recorder = context.recorder

    @contextmanager
    def _native(self):
        web_context = self.driver.current_context
        self.driver.switch_to.context(NATIVE_CONTEXT)
        try:
            yield
        finally:
            self.driver.switch_to.context(web_context)

    def navigate(self, path: str):
        try:
            self.driver.execute_script(LEAVE_PAGE_JS)
        except Exception:
            pass
        self.driver.get(path if path.startswith("http") else self.base_url + path)

    def open(self, path: str):
        self.navigate(path)
        not_loaded = _waited(lambda: _page_loading(self), OPEN_WAIT_S)
        if not_loaded:
            raise TimeoutError(f"open {path}: {not_loaded} after {OPEN_WAIT_S}s")

    def clean_state(self):
        self.open(CLEAN_PATH)
        self.driver.delete_all_cookies()
        self.driver.execute_script(CLEAR_STORAGE_JS)
        self.driver.get(BLANK_PAGE)

    def close_safari_tips(self):
        with self._native():
            closes = self.driver.find_elements(IOS_CLASS_CHAIN, CLOSE_BUTTONS_CHAIN)
            if not closes:
                return
            page_buttons = {button.id for button in
                            self.driver.find_elements(IOS_CLASS_CHAIN, WEB_CONTENT_BUTTONS_CHAIN)}
            for button in closes:
                if button.id not in page_buttons:
                    _close_unless_gone(button)

    def _native_tap(self, css: str):
        self.close_safari_tips()
        self.driver.find_element(CSS_SELECTOR, css).click()

    def _focus_missed(self, css: str) -> bool:
        state = self.driver.execute_script(FOCUS_STATE_JS, css)
        return bool(state) and state["focusable"] and not state["focused"]

    def tap(self, css: str):
        self._native_tap(css)
        if self._focus_missed(css):
            self._native_tap(css)
            self.recorder.tap_retries.append({"css": css, "focused_after_retry": not self._focus_missed(css)})

    def type(self, css: str, text: str):  # kwargs-lint: ignore: spec verb, selector then text is the spec format
        self.tap(css)
        self.wait_keyboard(up=True)
        try:
            self.driver.execute(KEYBOARD_TYPE_COMMAND, {"value": [text]})
        except Exception:
            self.save_native_tree("type_failed")
            raise

    def keyboard_up(self) -> bool:
        with self._native():
            return bool(self.driver.find_elements(CLASS_NAME, KEYBOARD_CLASS))

    def wait_keyboard(self, *, up: bool) -> bool:
        deadline = time.monotonic() + KEYBOARD_WAIT_S
        while time.monotonic() < deadline:
            if self.keyboard_up() == up:
                return True
            time.sleep(POLL_S)
        return False

    def dismiss_keyboard(self):
        self.driver.execute_script(BLUR_JS)
        if self.wait_keyboard(up=False):
            return
        try:
            self.driver.execute_script("mobile: hideKeyboard", {"keys": ["Done", "done"]})
        except Exception:
            return
        self.wait_keyboard(up=False)

    def rect(self, css: str):
        return self.driver.execute_script(RECT_JS, css)

    def viewport(self) -> dict:
        return self.driver.execute_script(VIEWPORT_JS)

    def js(self, script: str):
        return self.driver.execute_script(script)

    def swipe(self, css: str, from_share: float, to_share: float):  # kwargs-lint: ignore: spec verb, selector then span is the spec format
        line = self._swipe_line(css, (from_share, to_share))
        subprocess.run(["finger", os.environ[SIMULATOR_UDID_ENV], line.edge(), *line.screen_shares()],
                       check=True, capture_output=True, text=True, timeout=FINGER_TIMEOUT_S)
        time.sleep(SWIPE_SETTLE_S)

    def _swipe_line(self, css: str, span: tuple) -> SwipeLine:
        box = self.rect(css)
        if not box:
            raise LookupError(f"swipe {css}: no such element")
        with self._native():
            web_view = self.driver.find_element(CLASS_NAME, WEB_VIEW_CLASS).rect
            screen = self.driver.get_window_size()
        last_x = web_view["width"] - 1
        visible_middle = (max(box["top"], 0) + min(box["bottom"], web_view["height"])) / 2
        return SwipeLine(start_x=web_view["x"] + span[0] * last_x, end_x=web_view["x"] + span[1] * last_x,
                         y=web_view["y"] + visible_middle, screen=screen)

    def _bar_probe(self) -> dict:
        css_top = self.driver.execute_script(BAR_PROBE_JS, BAR_PROBE_ID, BAR_PROBE_CSS_HEIGHT)
        try:
            with self._native():
                native = self.driver.find_element(ACCESSIBILITY_ID, BAR_PROBE_ID).rect
        finally:
            self.driver.execute_script(REMOVE_PROBE_JS, BAR_PROBE_ID)
        return {"css_top": css_top, "css_height": BAR_PROBE_CSS_HEIGHT, "native_y": native["y"],
                "native_height": native["height"]}

    def safari_bottom_bar(self):
        probe = self._bar_probe()
        with self._native():
            screen = self.driver.get_window_size()
            web_view = self.driver.find_element(CLASS_NAME, WEB_VIEW_CLASS).rect
            native_xml = self.driver.page_source
        (self.recorder.out_dir / f"native_tree_bar_{len(self.recorder.shots):02d}.xml").write_text(native_xml, encoding="utf-8")
        bar_top = bottom_bar_top_pt(native_xml, screen_height=screen["height"])
        if bar_top is None:
            return None
        return {"top": css_y(bar_top, probe), "web_view_bottom": css_y(web_view["y"] + web_view["height"], probe),
                "screen_bottom": css_y(screen["height"], probe), "native_top": bar_top,
                "screen_height_pt": screen["height"]}

    def shot(self, name: str) -> Path:
        path = self.recorder.out_dir / "shots" / f"{len(self.recorder.shots) + 1:02d}_{name}.png"
        self.driver.get_screenshot_as_file(str(path))
        self.recorder.shots.append(path.name)
        return path

    def save_console(self) -> list:
        try:
            return self.driver.get_log("safariConsole")
        except Exception as error:
            return [{"level": "nexus", "message": f"console unavailable: {type(error).__name__}"}]

    def save_native_tree(self, name: str):
        with self._native():
            (self.recorder.out_dir / f"native_tree_{name}.xml").write_text(self.driver.page_source, encoding="utf-8")

    def spec_namespace(self) -> dict:
        verbs = {verb: getattr(self, verb) for verb in SPEC_VERBS}
        expect = lambda *fields, **named: self.recorder.record(Expectation(*fields, **named))  # noqa: E731
        return {"__name__": "iphone_spec", "expect": expect, **verbs}


def _keyboard_measure(phone: Phone, inner_height: float) -> dict:
    phone.tap(f"#{PROBE_INPUT_ID}")
    native_up = phone.wait_keyboard(up=True)
    shrink = inner_height - phone.viewport()["visual_height"]
    return {"ok": native_up and shrink >= MIN_KEYBOARD_SHRINK_PX, "native_keyboard": native_up, "shrink_px": shrink}


def _hardware_keyboard_off_by_simulator_menu():
    subprocess.run(["osascript", "-e", HARDWARE_KEYBOARD_OFF_SCRIPT], capture_output=True, text=True,
                   timeout=OSASCRIPT_TIMEOUT_S)


def _attempt(phone: Phone, method: str) -> dict:
    if method == "simulator_menu":
        phone.dismiss_keyboard()
        _hardware_keyboard_off_by_simulator_menu()
    inner_height = phone.viewport()["inner_height"]
    return {**_keyboard_measure(phone, inner_height), "method": method, "inner_height": inner_height}


def _no_keyboard_detail(last: dict) -> str:
    return (f"{KEYBOARD_NOT_SHOWING}: native keyboard {'visible' if last['native_keyboard'] else 'absent'}, "
            f"visualViewport shrank {last['shrink_px']:.0f}px (need {MIN_KEYBOARD_SHRINK_PX})")


def _page_loading(phone: Phone) -> Optional[str]:
    state = phone.driver.execute_script(PAGE_READY_JS)
    return None if state in READY_STATES else f"page still {state}"


def _page_state(phone: Phone) -> Optional[str]:
    asked = time.monotonic()
    loading = _page_loading(phone)
    answer_s = time.monotonic() - asked
    if loading:
        return loading
    return f"Safari still settling ({answer_s:.1f}s per script)" if answer_s > SAFARI_SETTLED_S else None


def _reason_or_none(check) -> Optional[str]:
    try:
        return check()
    except Exception as error:
        return f"page unreachable ({type(error).__name__})"


def _waited(check, wait_s: float) -> Optional[str]:
    deadline = time.monotonic() + wait_s
    seen = _reason_or_none(check)
    while seen and time.monotonic() < deadline:
        time.sleep(POLL_S)
        seen = _reason_or_none(check)
    return seen


def _probe_missing(phone: Phone) -> Optional[str]:
    present = phone.driver.execute_script(PROBE_PRESENT_JS, PROBE_INPUT_ID)
    return None if present else "the probe input never appeared on the page"


def _safari_not_open(phone: Phone, seen: str) -> dict:
    phone.shot("preflight_safari_not_open")
    phone.save_native_tree("preflight_failed")
    return {"ok": False, "detail": f"Safari not open: {seen} after {COLD_SAFARI_READY_WAIT_S}s", "attempts": []}


def keyboard_preflight(phone: Phone) -> dict:
    phone.navigate("/")
    not_open = _waited(lambda: _page_state(phone), COLD_SAFARI_READY_WAIT_S)
    if not not_open:
        phone.driver.execute_script(PROBE_INPUT_JS, PROBE_INPUT_ID)
        not_open = _waited(lambda: _probe_missing(phone), COLD_SAFARI_READY_WAIT_S)
    if not_open:
        return _safari_not_open(phone, not_open)
    return _keyboard_attempts(phone)


def _keyboard_attempts(phone: Phone) -> dict:
    attempts = []
    for method in ("capabilities", "simulator_menu"):
        attempts.append(_attempt(phone, method))
        if attempts[-1]["ok"]:
            phone.shot("preflight_keyboard_up")
            phone.dismiss_keyboard()
            phone.driver.execute_script(REMOVE_PROBE_JS, PROBE_INPUT_ID)
            return {"ok": True, "method": method, "detail": f"keyboard up via {method}", "attempts": attempts}
    phone.shot("preflight_no_keyboard")
    phone.save_native_tree("preflight_failed")
    return {"ok": False, "detail": _no_keyboard_detail(attempts[-1]), "attempts": attempts}
