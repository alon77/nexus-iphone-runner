"""harness — the only surface an iPhone spec touches (`guide iphone:spec_format`). A spec is a Python file at
experts/<expert>/tests/iphone/<name>.py whose top-level code calls these names, injected by serve:

    open(path)            Safari goes to the target host + path (or a full URL)
    tap(css)              closes any Safari tip bubble (it swallows the next tap), then a real native tap on the
                          element (nativeWebTap), so focus raises the keyboard; a tap that leaves a focusable
                          element unfocused is retried once and listed in results.json tap_retries
    type(css, text)       tap, then type through the on-screen keyboard
    keyboard_up()         True while a native XCUIElementTypeKeyboard is visible
    dismiss_keyboard()    hide the keyboard, wait until it is gone
    rect(css)             getBoundingClientRect as a dict (None when the element is missing)
    viewport()            visual_height, visual_offset_top, inner_height, inner_width, scroll_y
    shot(name)            full-screen simulator screenshot, keyboard included
    js(script)            run JavaScript in the page, `return` hands a value back (document.cookie, input values)
    expect(name, condition, detail)   one red/green line in results.json

Appium never leaks into a spec. Once per session, before any spec, keyboard_preflight proves the software
keyboard comes up; before each spec, clean_state wipes the site's cookies and storage.
"""

import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

KEYBOARD_CAPABILITIES = {"appium:connectHardwareKeyboard": False,
                         "appium:forceSimulatorSoftwareKeyboardPresence": True}
APPIUM_URL = "http://127.0.0.1:4723"
WDA_LAUNCH_TIMEOUT_MS = 600_000
NEW_COMMAND_TIMEOUT_S = 600
WEBVIEW_CONNECT_TIMEOUT_MS = 60_000
MIN_KEYBOARD_SHRINK_PX = 200
KEYBOARD_WAIT_S = 8
PAGE_READY_WAIT_S = 30
TYPING_KEYS_PER_MINUTE = 600
OSASCRIPT_TIMEOUT_S = 30
POLL_S = 0.25
NATIVE_CONTEXT = "NATIVE_APP"
CSS_SELECTOR = "css selector"
CLASS_NAME = "class name"
IOS_CLASS_CHAIN = "-ios class chain"
CLOSE_BUTTONS_CHAIN = ('**/XCUIElementTypeButton[`name IN {"Close", "Not Now"} OR label IN {"Close", "Not Now"}`]')
WEB_CONTENT_BUTTONS_CHAIN = "**/XCUIElementTypeWebView/**/XCUIElementTypeButton"
KEYBOARD_CLASS = "XCUIElementTypeKeyboard"
PROBE_INPUT_ID = "nexus_keyboard_probe"
KEYBOARD_NOT_SHOWING = "software keyboard not showing"
SPEC_VERBS = ("open", "tap", "type", "keyboard_up", "dismiss_keyboard", "rect", "viewport", "shot", "js")
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
PAGE_READY_JS = "return document.readyState;"
READY_STATES = ("interactive", "complete")
WEBVIEW_PREFIX = "WEBVIEW"
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
            "appium:newCommandTimeout": NEW_COMMAND_TIMEOUT_S, "appium:showXcodeLog": True,
            "appium:webviewConnectTimeout": WEBVIEW_CONNECT_TIMEOUT_MS,
            "appium:maxTypingFrequency": TYPING_KEYS_PER_MINUTE, "pageLoadStrategy": "eager", **KEYBOARD_CAPABILITIES}


def prebuilt_wda_capabilities(app_path: str) -> dict:
    return {"appium:usePreinstalledWDA": True, "appium:prebuiltWDAPath": app_path}


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
class RunContext:
    driver: object
    base_url: str
    recorder: Recorder


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

    def open(self, path: str):
        self.driver.get(path if path.startswith("http") else self.base_url + path)

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
                if button.id not in page_buttons and button.is_displayed():
                    button.click()

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
        with self._native():
            self.driver.execute_script("mobile: keys", {"keys": list(text)})

    def keyboard_up(self) -> bool:
        with self._native():
            keyboards = self.driver.find_elements(CLASS_NAME, KEYBOARD_CLASS)
            return any(keyboard.is_displayed() for keyboard in keyboards)

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


def _page_state(phone: Phone) -> Optional[str]:
    contexts = list(phone.driver.contexts)
    if not any(name.startswith(WEBVIEW_PREFIX) for name in contexts):
        return f"no Safari web view (contexts {contexts})"
    try:
        state = phone.driver.execute_script(PAGE_READY_JS)
    except Exception as error:
        return f"page unreachable ({type(error).__name__})"
    return None if state in READY_STATES else f"page still {state}"


def _waited(check) -> Optional[str]:
    deadline = time.monotonic() + PAGE_READY_WAIT_S
    seen = check()
    while seen and time.monotonic() < deadline:
        time.sleep(POLL_S)
        seen = check()
    return seen


def _probe_missing(phone: Phone) -> Optional[str]:
    present = phone.driver.execute_script(PROBE_PRESENT_JS, PROBE_INPUT_ID)
    return None if present else "the probe input never appeared on the page"


def _safari_not_open(phone: Phone, seen: str) -> dict:
    phone.shot("preflight_safari_not_open")
    phone.save_native_tree("preflight_failed")
    return {"ok": False, "detail": f"Safari not open: {seen} after {PAGE_READY_WAIT_S}s", "attempts": []}


def keyboard_preflight(phone: Phone) -> dict:
    phone.open("/")
    not_open = _waited(lambda: _page_state(phone))
    if not not_open:
        phone.driver.execute_script(PROBE_INPUT_JS, PROBE_INPUT_ID)
        not_open = _waited(lambda: _probe_missing(phone))
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
