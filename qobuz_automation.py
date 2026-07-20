import math
import subprocess
import time

import ApplicationServices as AS
import AppKit
import Quartz

from qobuz_integration import get_qobuz_snapshot, qobuz_is_running


QOBUZ_BUNDLE_ID = "com.qobuz.desktop"


class QobuzAutomationError(RuntimeError):
    pass


def core_graphics_click(x, y):
    point = (float(x), float(y))
    events = (
        (Quartz.kCGEventMouseMoved, Quartz.kCGMouseButtonLeft),
        (Quartz.kCGEventLeftMouseDown, Quartz.kCGMouseButtonLeft),
        (Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft),
    )
    for index, (event_type, button) in enumerate(events):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, point, button)
        if event is None:
            raise QobuzAutomationError("CoreGraphicsマウスイベントを作成できません")
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        if index == 0:
            time.sleep(0.2)


def core_graphics_key(keycode):
    for is_down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, int(keycode), is_down)
        if event is None:
            raise QobuzAutomationError("CoreGraphicsキーイベントを作成できません")
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _normalized(value):
    return " ".join(str(value or "").split()).strip()


class MacOSQobuzAccessibility:
    """Read Qobuz AX elements in-process so one app permission is sufficient."""

    def __init__(self):
        self._prompted = False

    def _running_application(self):
        applications = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            QOBUZ_BUNDLE_ID
        )
        if not applications:
            raise QobuzAutomationError("Qobuzプロセスを取得できません")
        return applications[0]

    def activate(self):
        application = self._running_application()
        options = (
            AppKit.NSApplicationActivateAllWindows
            | AppKit.NSApplicationActivateIgnoringOtherApps
        )
        if not application.activateWithOptions_(options):
            raise QobuzAutomationError("Qobuzを前面にできません")

    @staticmethod
    def _attribute(element, attribute, default=None):
        try:
            error, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
        except Exception:
            return default
        if error != AS.kAXErrorSuccess or value is None:
            return default
        return value

    def _main_window(self):
        application = self._running_application()
        app_element = AS.AXUIElementCreateApplication(application.processIdentifier())
        
        for _ in range(15):
            main_window = self._attribute(app_element, AS.kAXMainWindowAttribute)
            if main_window:
                return main_window
            windows = self._attribute(app_element, AS.kAXWindowsAttribute, ())
            if windows:
                for window in windows:
                    if self._attribute(window, AS.kAXRoleAttribute) == AS.kAXWindowRole:
                        return window
                return windows[0]
            
            subprocess.run(["open", "-a", "Qobuz"], check=False)
            time.sleep(0.2)
            
        raise QobuzAutomationError("Qobuzウィンドウを取得できません。Qobuzの画面が開いているか確認してください")

    def check(self):
        if not AS.AXIsProcessTrusted():
            if not self._prompted:
                AS.AXIsProcessTrustedWithOptions(
                    {AS.kAXTrustedCheckOptionPrompt: True}
                )
                self._prompted = True
            raise QobuzAutomationError(
                "Hi-Res Recorderのアクセシビリティ許可が必要です。\n"
                "システム設定で許可後、アプリを再起動してください。\n"
                "（ループする場合はシステム設定から一度「-」で削除し、再度追加してください）"
            )
        role = self._attribute(self._main_window(), AS.kAXRoleAttribute)
        if role != AS.kAXWindowRole:
            raise QobuzAutomationError("QobuzのAccessibility要素を取得できません")
        return True

    @staticmethod
    def _sequence(value):
        if value is None:
            return ()
        try:
            return tuple(value)
        except TypeError:
            return ()

    def _children(self, element):
        children = list(
            self._sequence(self._attribute(element, AS.kAXChildrenAttribute, ()))
        )
        rows = self._sequence(self._attribute(element, AS.kAXRowsAttribute, ()))
        children.extend(rows)
        return children

    def _iter_items(self, limit=20000):
        pending = [self._main_window()]
        count = 0
        while pending:
            element = pending.pop()
            yield element
            count += 1
            if count > limit:
                break
            pending.extend(reversed(self._children(element)))

    def _all_items(self, limit=20000):
        items = list(self._iter_items(limit=limit))
        if len(items) >= limit:
            raise QobuzAutomationError("Qobuz Accessibility要素数が上限を超えました")
        return items

    def _role(self, element):
        return self._attribute(element, AS.kAXRoleAttribute, "")

    def _texts(self, element):
        values = []
        for attribute in (
            AS.kAXTitleAttribute,
            AS.kAXDescriptionAttribute,
            AS.kAXValueAttribute,
        ):
            value = self._attribute(element, attribute)
            if isinstance(value, str):
                text = _normalized(value)
                if text and text not in values:
                    values.append(text)
        return tuple(values)

    def _point_value(self, element, attribute, value_type):
        value = self._attribute(element, attribute)
        if value is None:
            return None
        try:
            result = AS.AXValueGetValue(value, value_type, None)
            if (
                isinstance(result, tuple)
                and len(result) == 2
                and isinstance(result[0], (bool, int))
            ):
                success, point = result
                if not success:
                    return None
            else:
                point = result
            if hasattr(point, "x"):
                return float(point.x), float(point.y)
            return float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError, AttributeError):
            return None

    def _frame(self, element):
        position = self._point_value(
            element, AS.kAXPositionAttribute, AS.kAXValueCGPointType
        )
        size = self._point_value(element, AS.kAXSizeAttribute, AS.kAXValueCGSizeType)
        if position is None or size is None:
            return None
        x, y = position
        width, height = size
        values = (x, y, width, height)
        if not all(math.isfinite(value) for value in values):
            return None
        return values

    def _center(self, element):
        frame = self._frame(element)
        if frame is None or frame[2] <= 0 or frame[3] <= 0:
            return None
        x, y, width, height = frame
        return x + (width / 2.0), y + (height / 2.0)

    def _find_exact_text_iter(self, items, target, role=None, visible=False):
        target = _normalized(target)
        for item in items:
            if role and self._role(item) != role:
                continue
            if target not in self._texts(item):
                continue
            if visible and self._center(item) is None:
                continue
            yield item

    def _find_exact_text(self, items, target, role=None, visible=False):
        return list(self._find_exact_text_iter(items, target, role=role, visible=visible))

    def track_play_point(self, title, track_number):
        self.activate()
        label = None
        for _ in range(20):
            for item in self._find_exact_text_iter(self._iter_items(limit=10000), title):
                if self._center(item):
                    label = item
                    break
                if label is None:
                    label = item
            if label:
                break
            time.sleep(0.5)
        if not label:
            raise QobuzAutomationError("Qobuzアルバム画面に対象曲が見つかりません")
        try:
            AS.AXUIElementPerformAction(
                label, AS.NSAccessibilityScrollToVisibleAction
            )
        except Exception:
            pass
        time.sleep(0.5)

        visible_label = None
        for _ in range(10):
            for item in self._find_exact_text_iter(self._iter_items(limit=10000), title, visible=True):
                visible_label = item
                break
            if visible_label:
                break
            time.sleep(0.5)
        if not visible_label:
            raise QobuzAutomationError("Qobuz対象曲を表示領域へ移動できません")
            
        label_frame = self._frame(visible_label)
        label_x, label_y = label_frame[0], label_frame[1]
        target_number = _normalized(track_number)
        buttons = []
        for _ in range(10):
            for item in self._iter_items(limit=10000):
                if self._role(item) != AS.kAXButtonRole:
                    continue
                frame = self._frame(item)
                if frame is None or frame[2] <= 0 or frame[3] <= 0:
                    continue
                if frame[0] >= label_x or abs(frame[1] - label_y) > 24:
                    continue
                texts = tuple(text.lower() for text in self._texts(item))
                if not any(
                    text in {target_number.lower(), "再生", "play"} for text in texts
                ):
                    continue
                buttons.append((frame[0], item))
                if len(buttons) >= 5:
                    break
            if buttons:
                break
            time.sleep(0.5)
        if not buttons:
            raise QobuzAutomationError("Qobuz対象曲の再生ボタンが見つかりません")
        buttons.sort(key=lambda entry: entry[0], reverse=True)
        return self._center(buttons[0][1])

    def output_button_point(self):
        self.activate()
        for _ in range(20):
            for match in self._find_exact_text_iter(
                self._iter_items(limit=10000), "\ue903", role=AS.kAXStaticTextRole, visible=True
            ):
                return self._center(match)
            time.sleep(0.5)
        raise QobuzAutomationError("Qobuzのオーディオ出力ボタンが見つかりません")

    def output_device_point(self, device_name):
        for _ in range(20):
            for match in self._find_exact_text_iter(
                self._iter_items(limit=10000), device_name, role=AS.kAXStaticTextRole, visible=True
            ):
                return self._center(match)
            time.sleep(0.5)
        raise QobuzAutomationError(
            f"Qobuzの出力一覧に{device_name}が見つかりません"
        )

    def visible_text_diagnostics(self, limit=80):
        diagnostics = []
        for item in self._all_items():
            texts = self._texts(item)
            frame = self._frame(item)
            if not texts or frame is None or frame[2] <= 0 or frame[3] <= 0:
                continue
            diagnostics.append(
                {"role": self._role(item), "texts": list(texts), "frame": list(frame)}
            )
        diagnostics.sort(key=lambda entry: (entry["frame"][1], entry["frame"][0]), reverse=True)
        return diagnostics[: int(limit)]

    def raw_diagnostics(self, limit=30):
        items = self._all_items()
        samples = []
        for item in items:
            texts = self._texts(item)
            if not texts:
                continue
            position = self._attribute(item, AS.kAXPositionAttribute)
            size = self._attribute(item, AS.kAXSizeAttribute)
            samples.append(
                {
                    "role": self._role(item),
                    "texts": list(texts),
                    "position_type": type(position).__name__,
                    "position": repr(position),
                    "size_type": type(size).__name__,
                    "size": repr(size),
                    "frame": self._frame(item),
                }
            )
            if len(samples) >= int(limit):
                break
        return {"item_count": len(items), "text_samples": samples}


class QobuzAutomation:
    """Control visible Qobuz UI without private APIs or child AX processes."""

    def __init__(
        self,
        runner=None,
        snapshot_reader=None,
        sleep=None,
        point_clicker=None,
        key_presser=None,
        accessibility=None,
    ):
        self.runner = runner or subprocess.run
        self.snapshot_reader = snapshot_reader or get_qobuz_snapshot
        self.sleep = sleep or time.sleep
        self.point_clicker = point_clicker or core_graphics_click
        self.key_presser = key_presser or core_graphics_key
        self.accessibility = accessibility or MacOSQobuzAccessibility()

    def _run(self, command, timeout=8.0):
        try:
            result = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise QobuzAutomationError(str(exc)) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise QobuzAutomationError(detail)
        return (result.stdout or "").strip()

    def ensure_running(self):
        running = qobuz_is_running()
        needs_restart = False

        if running:
            try:
                has_web_area = False
                for item in self.accessibility._iter_items(limit=1000):
                    if self.accessibility._role(item) == AS.kAXWebAreaRole:
                        has_web_area = True
                        break
                if not has_web_area:
                    needs_restart = True
            except Exception:
                needs_restart = True
                
        if needs_restart:
            try:
                subprocess.run(["killall", "Qobuz"], check=False, capture_output=True)
            except Exception:
                pass
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not qobuz_is_running():
                    break
                self.sleep(0.5)
            running = False

        if not running:
            self._run(["open", "-a", "Qobuz", "--args", "--force-renderer-accessibility"])
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if qobuz_is_running():
                    break
                self.sleep(0.5)
            else:
                raise QobuzAutomationError("Qobuzアプリを起動できません")
            self.sleep(4.0)

    def check_accessibility(self):
        self.ensure_running()
        return self.accessibility.check()

    def activate(self):
        self.accessibility.activate()

    def open_track(self, track_id, album_id=None, track_number=None, title=None):
        self.ensure_running()
        if not album_id or track_number is None or not title:
            raise QobuzAutomationError(
                "Qobuz曲の公開アルバムURL・曲番号・曲名が不足しています"
            )
        self._run(["open", f"qobuzapp://album/{album_id}"])
        self.sleep(1.0)
        point = self.accessibility.track_play_point(title, track_number)
        self.point_clicker(*point)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            snapshot = self.snapshot_reader()
            if str(snapshot.get("track_id") or "") == str(track_id):
                try:
                    position = float(snapshot.get("position") or 0.0)
                except (TypeError, ValueError):
                    position = 0.0
                if (
                    str(snapshot.get("state", "")).lower() == "playing"
                    and position <= 3.0
                ):
                    return snapshot
            self.sleep(0.2)
        raise QobuzAutomationError(
            f"Qobuz曲頭再生を検証できません: track {track_id}"
        )

    def _press_space(self):
        self.accessibility.activate()
        self.sleep(0.15)
        self.key_presser(49)

    def pause(self):
        snapshot = self.snapshot_reader()
        if str(snapshot.get("state", "")).lower() == "playing":
            self._press_space()

    def play(self):
        snapshot = self.snapshot_reader()
        if str(snapshot.get("state", "")).lower() != "playing":
            self._press_space()

    def select_output_device(self, device_name):
        point = self.accessibility.output_button_point()
        self.point_clicker(*point)
        self.sleep(0.5)
        point = self.accessibility.output_device_point(device_name)
        self.point_clicker(*point)
        self.sleep(0.5)
        deadline = time.monotonic() + 8.0
        observed = ""
        while time.monotonic() < deadline:
            snapshot = self.snapshot_reader()
            observed = str(snapshot.get("output_device_name") or "")
            if observed.strip().lower() == str(device_name).strip().lower():
                return
            self.sleep(0.2)
        raise QobuzAutomationError(
            f"Qobuz出力先の変更を検証できません: {observed or 'unknown'}"
        )

    def snapshot_state(self):
        snapshot = self.snapshot_reader()
        return {
            "track_id": snapshot.get("track_id"),
            "album_id": snapshot.get("album_id"),
            "track_number": snapshot.get("track_number"),
            "title": snapshot.get("name") or snapshot.get("title"),
            "state": snapshot.get("state"),
            "position": snapshot.get("position"),
            "output_device_name": snapshot.get("output_device_name"),
            "volume_percent": snapshot.get("volume_percent"),
            "muted": snapshot.get("muted"),
        }

    def prepare_job(self, output_device_name):
        self.check_accessibility()
        before = self.snapshot_state()
        self.pause()
        if str(before.get("output_device_name") or "").strip().lower() != str(
            output_device_name
        ).strip().lower():
            self.select_output_device(output_device_name)
        return before

    def restore_state(self, state):
        errors = []
        output = (state or {}).get("output_device_name")
        if output:
            try:
                current = self.snapshot_state().get("output_device_name")
                if str(current or "").strip().lower() != str(output).strip().lower():
                    self.select_output_device(output)
            except Exception as exc:
                errors.append(f"Qobuz出力先復元失敗: {exc}")
        track_id = (state or {}).get("track_id")
        if track_id:
            try:
                self.open_track(
                    track_id,
                    album_id=(state or {}).get("album_id"),
                    track_number=(state or {}).get("track_number"),
                    title=(state or {}).get("title"),
                )
                self.sleep(0.8)
                if str((state or {}).get("state", "")).lower() == "playing":
                    self.play()
                else:
                    self.pause()
            except Exception as exc:
                errors.append(f"Qobuz再生状態復元失敗: {exc}")
        return {"restored": not errors, "errors": errors}
