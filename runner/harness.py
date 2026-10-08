"""harness — the only surface an iPhone spec touches (`guide iphone:spec_format`). A spec is a Python file at
experts/<expert>/tests/iphone/<name>.py whose top-level code calls these names, injected by run_spec:

    open(path)            Safari goes to the target host + path (or a full URL)
    tap(css)              a real native tap on the element (nativeWebTap), so focus raises the keyboard
    type(css, text)       tap, then type through the on-screen keyboard
    keyboard_up()         True while a native XCUIElementTypeKeyboard is visible
    dismiss_keyboard()    hide the keyboard, wait until it is gone
    rect(css)             getBoundingClientRect as a dict (None when the element is missing)
    viewport()            visual_height, visual_offset_top, inner_height, inner_width, scroll_y
    shot(name)            full-screen simulator screenshot, keyboard included
    js(script)            run JavaScript in the page, `return` hands a value back (document.cookie, input values)
    expect(name, condition, detail)   one red/green line in results.json

Appium never leaks into a spec. Before any spec step, keyboard_preflight proves the software keyboard comes up.
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
OSASCRIPT_TIMEOUT_S = 30
POLL_S = 0.25
NATIVE_CONTEXT = "NATIVE_APP"
KEYBOARD_CLASS = "XCUIElementTypeKeyboard"
FOCUSED_FIELD = "hasKeyboardFocus == 1"
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
BLUR_JS = "if (document.activeElement) document.activeElement.blur();"
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
            "appium:webviewConnectTimeout": WEBVIEW_CONNECT_TIMEOUT_MS, **KEYBOARD_CAPABILITIES}


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

    def tap(self, css: str):
        from selenium.webdriver.common.by import By
        self.driver.find_element(By.CSS_SELECTOR, css).click()

    def type(self, css: str, text: str):  # kwargs-lint: ignore: spec verb, selector then text is the spec format
        from appium.webdriver.common.appiumby import AppiumBy
        self.tap(css)
        self.wait_keyboard(up=True)
        with self._native():
            self.driver.find_element(AppiumBy.IOS_PREDICATE, FOCUSED_FIELD).send_keys(text)

    def keyboard_up(self) -> bool:
        from appium.webdriver.common.appiumby import AppiumBy
        with self._native():
            keyboards = self.driver.find_elements(AppiumBy.CLASS_NAME, KEYBOARD_CLASS)
            return any(keyboard.is_displayed() for keyboard in keyboards)

    def wait_keyboard(self, *, up: bool) -> bool:
        deadline = time.monotonic() + KEYBOARD_WAIT_S
        while time.monotonic() < deadline:
            if self.keyboard_up() == up:
                return True
            time.sleep(POLL_S)
        return False

    def dismiss_keyboard(self):
        try:
            self.driver.execute_script("mobile: hideKeyboard", {"keys": ["Done", "done"]})
        except Exception:
            self.driver.execute_script(BLUR_JS)
        if not self.wait_keyboard(up=False):
            self.driver.execute_script(BLUR_JS)
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


def keyboard_preflight(phone: Phone) -> dict:
    phone.open("/")
    phone.driver.execute_script(PROBE_INPUT_JS, PROBE_INPUT_ID)
    attempts = []
    for method in ("capabilities", "simulator_menu"):
        attempts.append(_attempt(phone, method))
        if attempts[-1]["ok"]:
            phone.shot("preflight_keyboard_up")
            phone.dismiss_keyboard()
            phone.driver.execute_script(REMOVE_PROBE_JS, PROBE_INPUT_ID)
            return {"ok": True, "method": method, "detail": f"keyboard up via {method}", "attempts": attempts}
    phone.shot("preflight_no_keyboard")
    return {"ok": False, "detail": _no_keyboard_detail(attempts[-1]), "attempts": attempts}
