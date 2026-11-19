"""校企E站自动登录：Playwright 驱动 + 验证码识别。

流程：打开登录页 -> 选择单位 -> 填写账号密码 -> 抓验证码/识别/回填 -> 提交登录。
支持失败自动换验证码重试，以及手动停止。
"""
from __future__ import annotations

import base64
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from pyapp.captcha import recognize

STEPS = [
    "打开登录页",
    "选择单位",
    "填写账号密码",
    "识别验证码",
    "提交登录",
    "打开实习平台",
    "点击实习过程",
    "打开周日志批阅",
    "切到周志",
    "逐个批阅周志",
]

SELECTORS = {
    "unit": 'input[placeholder="请选择学校"]',
    "account": 'input[placeholder="账号"]',
    "password": 'input[placeholder="密码"]',
    "captcha_input": 'input[placeholder="验证码"]',
    "captcha_img": "img.login-code-img",
    "login_btn": "button.el-button--primary",
}

DEFAULT_INTERN_TEXT = "实习平台"
BROWSER_LABELS = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", None: "内置 Chromium"}
SCORE_OVERRIDE_RE = re.compile(r"^\s*(.+?)\s*[=＝:：,，\t ]+\s*(\d+(?:\.\d+)?)\s*$")

MAX_LOG = 400


class StopRequested(Exception):
    pass


class LoginRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pw = None
        self._browser = None
        self._page = None
        self.state: Dict[str, Any] = {
            "running": False,
            "result": "idle",
            "message": "",
            "step": 0,
            "step_text": "",
            "steps": STEPS,
            "attempt": 0,
            "max_attempts": 0,
            "captcha": None,
            "intern_url": "",
            "approved": [],
            "logs": [],
            "browser_open": False,
        }

    # ---------- 状态与日志 ----------
    def log(self, level: str, msg: str) -> None:
        with self._lock:
            self.state["logs"].append(
                {"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg}
            )
            if len(self.state["logs"]) > MAX_LOG:
                del self.state["logs"][: len(self.state["logs"]) - MAX_LOG]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            data = dict(self.state)
            data["logs"] = list(self.state["logs"])
            data["steps"] = list(STEPS)
            return data

    def _set(self, **kwargs: Any) -> None:
        with self._lock:
            self.state.update(kwargs)

    def _step(self, index: int, text: str = "") -> None:
        self._set(step=index, step_text=text or STEPS[index - 1])
        self.log("step", f"[{index}/{len(STEPS)}] {STEPS[index - 1]}")

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise StopRequested()

    # ---------- 生命周期 ----------
    def start(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        if self.state["running"]:
            return {"ok": False, "error": "已有任务在执行中"}
        self._stop.clear()
        self._set(
            running=True,
            result="running",
            message="",
            step=0,
            step_text="准备中",
            attempt=0,
            captcha=None,
            intern_url="",
            approved=[],
            logs=[],
        )
        self._thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self._thread.start()
        return {"ok": True}

    def stop(self) -> None:
        self._stop.set()
        self.log("warn", "已请求停止，等待当前动作结束…")
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass

    def close_browser(self) -> None:
        self._shutdown()
        self._set(browser_open=False, result="idle" if self.state["result"] == "success" else self.state["result"])

    def _shutdown(self) -> None:
        for attr in ("_browser", "_page"):
            setattr(self, attr, None)
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

    # ---------- 主流程 ----------
    def _run(self, cfg: Dict[str, Any]) -> None:
        settings = cfg.get("settings") or {}
        max_attempts = int(settings.get("max_attempts") or 6)
        min_conf = float(settings.get("min_confidence") or 0.35)
        slow_mo = int(settings.get("slow_mo") or 0)
        headless = bool(settings.get("headless"))
        url = cfg.get("url") or settings.get("url") or "https://syuantech.cn/login_1?redirect=%2FcontrolBoard%7D"
        unit = (cfg.get("unit") or "").strip()
        account = (cfg.get("account") or "").strip()
        password = cfg.get("password") or ""
        intern_text = (settings.get("intern_text") or DEFAULT_INTERN_TEXT).strip() or DEFAULT_INTERN_TEXT
        approve_enabled = bool(settings.get("approve_enabled", True))
        approve_score = str(settings.get("approve_score") or "99").strip() or "99"
        approve_comment = str(settings.get("approve_comment") or "加油")
        try:
            approve_delay = int(settings.get("approve_delay", 800))
        except (TypeError, ValueError):
            approve_delay = 800
        try:
            approve_limit = int(settings.get("approve_limit", 200))
        except (TypeError, ValueError):
            approve_limit = 200
        score_overrides = self.parse_score_overrides(settings.get("score_overrides") or "")
        browser_pref = str(settings.get("browser") or "auto").strip().lower()
        if browser_pref not in ("chrome", "msedge"):
            browser_pref = None
        if score_overrides:
            self.log(
                "info",
                "指定分数：" + "，".join(f"{k}={v}" for k, v in score_overrides.items()),
            )
        try:
            viewport_w = int(settings.get("viewport_w") or 1440)
        except (TypeError, ValueError):
            viewport_w = 1440
        try:
            viewport_h = int(settings.get("viewport_h") or 900)
        except (TypeError, ValueError):
            viewport_h = 900
        viewport_w = max(1024, min(viewport_w, 3840))
        viewport_h = max(680, min(viewport_h, 2160))

        self._set(max_attempts=max_attempts)
        try:
            self._shutdown()
            self._pw = sync_playwright().start()
            self._browser, used_channel = self._launch(
                self._pw, headless, slow_mo, browser_pref, (viewport_w, viewport_h)
            )
            self._set(browser_open=True)
            self.log("info", f"使用浏览器：{BROWSER_LABELS.get(used_channel, str(used_channel))}")
            context = self._browser.new_context(
                viewport={"width": viewport_w, "height": viewport_h}, locale="zh-CN"
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            self._page = page
            self.log("info", f"页面视口：{viewport_w}×{viewport_h}（固定，不受窗口大小影响）")

            # 1 打开页面
            self._step(1)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector(SELECTORS["unit"], timeout=25000)
            page.wait_for_timeout(800)
            self.log("ok", f"页面已加载：{page.title()}")
            self._check_stop()

            # 2 选择单位
            self._step(2)
            self._select_unit(page, unit)
            self.log("ok", f"单位已选择：{unit}")
            self._check_stop()

            # 3 账号密码
            self._step(3)
            page.fill(SELECTORS["account"], account)
            page.fill(SELECTORS["password"], password)
            self.log("ok", f"账号已填写：{account}（密码 {len(password)} 位）")
            self._check_stop()

            # 4 + 5 验证码与提交（可重试）
            for attempt in range(1, max_attempts + 1):
                self._check_stop()
                self._set(attempt=attempt)
                self._step(4, f"识别验证码（第 {attempt}/{max_attempts} 次）")
                image, result = self._solve_captcha(page, min_conf, fresh=attempt > 1)
                self._set(
                    captcha={
                        "image": "data:image/jpeg;base64,"
                        + base64.b64encode(image).decode("ascii"),
                        "text": result.text,
                        "answer": result.answer,
                        "confidence": round(result.confidence, 3),
                    }
                )
                self.log(
                    "ok",
                    f"识别结果：{result.text} → {result.answer}（置信度 {result.confidence:.2f}）",
                )

                self._step(5, f"提交登录（第 {attempt}/{max_attempts} 次）")
                page.fill(SELECTORS["captcha_input"], result.answer)
                outcome, message = self._submit(page)
                if outcome == "success":
                    self.log("ok", f"登录成功，已进入：{page.url}")
                    self._set(step=5, step_text="登录成功")
                    self._check_stop()

                    # 6 打开实习平台（站点用 window.open 开新标签页）
                    self._step(6)
                    try:
                        intern_url, intern_title = self._open_intern_page(page, intern_text)
                        self.log("ok", f"实习平台已在新标签页打开：{intern_url}")
                        if intern_title:
                            self.log("ok", f"标签页标题：{intern_title}")
                    except Exception as exc:  # noqa: BLE001
                        self.log("warn", f"实习平台未能自动打开：{exc}")
                        self._set(
                            result="success",
                            message=f"登录成功，但没打开实习平台：{exc}",
                            step=5,
                            step_text="登录成功（实习平台未打开）",
                            running=False,
                        )
                        return

                    # 7~9 实习过程 -> 周日志批阅 -> 周志
                    try:
                        summary = self._open_weekly_log(self.page)
                    except Exception as exc:  # noqa: BLE001
                        self.log("warn", f"后续导航未完成：{exc}")
                        self._set(
                            result="success",
                            message=f"已打开实习平台，但后续导航失败：{exc}",
                            intern_url=self.page.url,
                            step=6,
                            step_text="已打开实习平台（后续未完成）",
                            running=False,
                        )
                        return

                    approved: List[str] = []
                    if approve_enabled:
                        # 10 逐个批阅
                        self._step(10)
                        try:
                            approved = self._approve_all(
                                self.page, approve_score, approve_comment,
                                approve_delay, approve_limit, score_overrides,
                            )
                        except StopRequested:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            self.log("err", f"批阅中断：{exc}")

                    message = f"已进入周志批阅列表{summary}"
                    if approve_enabled:
                        message += (
                            f"，逐个通过 {len(approved)} 篇"
                            f"（分数 {approve_score}，评语「{approve_comment}」）"
                        )
                        if score_overrides:
                            message += "；指定分数：" + "，".join(
                                f"{k}={v}" for k, v in score_overrides.items()
                            )
                    self._set(
                        result="success",
                        message=message,
                        intern_url=self.page.url,
                        approved=approved,
                        step=len(STEPS),
                        step_text="已完成" if approve_enabled else "已到周志",
                        running=False,
                    )
                    return
                if outcome == "retry":
                    self.log("warn", f"提交未通过：{message}，换一张验证码重试")
                    continue
                self.log("err", f"登录失败：{message}")
                self._set(result="failed", message=message, running=False)
                return

            self.log("err", f"连续 {max_attempts} 次验证码均未通过，已停止")
            self._set(result="failed", message=f"连续 {max_attempts} 次未通过", running=False)

        except StopRequested:
            self.log("warn", "任务已停止")
            self._set(result="stopped", message="已手动停止", running=False)
        except PWError as exc:
            if self._stop.is_set():
                self.log("warn", "任务已停止")
                self._set(result="stopped", message="已手动停止", running=False)
            else:
                self.log("err", f"浏览器错误：{exc}")
                self._set(result="failed", message=str(exc).splitlines()[0], running=False)
        except Exception as exc:  # noqa: BLE001
            if self._stop.is_set():
                self.log("warn", "任务已停止")
                self._set(result="stopped", message="已手动停止", running=False)
            else:
                self.log("err", f"执行异常：{exc}")
                self._set(result="failed", message=str(exc), running=False)
        finally:
            self._stop.clear()

    @staticmethod
    def _launch(pw, headless: bool, slow_mo: int, prefer: Optional[str] = None,
                viewport: tuple = (1440, 900)):
        """启动浏览器，返回 (browser, channel)。channel 为 None 表示用的是 Playwright 内置 Chromium。

        顺序：用户指定的 → Chrome → Edge → 内置 Chromium（没装 Playwright 内核时前两个仍可用）。
        """
        width, height = viewport
        kwargs = dict(
            headless=headless,
            slow_mo=slow_mo,
            args=[
                # 窗口按视口大小开，避免小窗口下站点把菜单折叠进「更多菜单」
                f"--window-size={width},{height + 120}",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        order: List[Optional[str]] = []
        if prefer in ("chrome", "msedge"):
            order.append(prefer)
        for channel in ("chrome", "msedge"):
            if channel not in order:
                order.append(channel)
        order.append(None)

        last: Optional[Exception] = None
        for channel in order:
            try:
                if channel:
                    browser = pw.chromium.launch(channel=channel, **kwargs)
                else:
                    browser = pw.chromium.launch(**kwargs)
                return browser, channel
            except Exception as exc:  # noqa: BLE001, PERF203
                last = exc
                continue
        raise RuntimeError(f"无法启动浏览器（Chrome / Edge / 内置 Chromium 都失败）：{last}")

    # ---------- 具体动作 ----------
    def _select_unit(self, page, unit: str) -> None:
        box = page.locator(SELECTORS["unit"]).first
        box.click()
        page.wait_for_timeout(250)
        try:
            box.fill("")
            box.type(unit[:12], delay=30)
        except PWError:
            page.keyboard.type(unit[:12], delay=30)
        page.wait_for_timeout(700)

        item = page.locator(".el-select-dropdown:visible li").filter(has_text=unit).first
        try:
            item.click(timeout=4000)
        except PWError:
            page.keyboard.press("Escape")
            box.click()
            page.wait_for_timeout(300)
            page.locator(".el-select-dropdown:visible li").filter(has_text=unit).first.click(
                timeout=5000
            )
        page.wait_for_timeout(400)
        selected = (box.input_value() or "").strip()
        if selected != unit:
            raise RuntimeError(f"单位选择失败，当前值：{selected or '空'}")

    def _grab_captcha(self, page, fresh: bool) -> bytes:
        img = page.locator(SELECTORS["captcha_img"]).first
        before = img.get_attribute("src")
        if fresh:
            try:
                img.click()
            except PWError:
                pass
            for _ in range(40):
                page.wait_for_timeout(120)
                now = img.get_attribute("src")
                if now and now != before:
                    break
        src = img.get_attribute("src") or ""
        if src.startswith("data:"):
            payload = src.split(",", 1)[-1]
            return base64.b64decode(payload)
        # 普通 URL：走浏览器上下文请求，保持 Cookie 一致
        resp = page.request.get(src)
        return resp.body()

    def _solve_captcha(self, page, min_conf: float, fresh: bool):
        image = self._grab_captcha(page, fresh=fresh)
        result = recognize(image)
        if result.ok and result.confidence >= min_conf:
            return image, result
        for _ in range(3):
            self.log(
                "warn",
                f"识别不确定（{result.text or result.error or '空'}，"
                f"置信度 {result.confidence:.2f}），重新获取验证码",
            )
            image = self._grab_captcha(page, fresh=True)
            result = recognize(image)
            if result.ok and result.confidence >= min_conf:
                return image, result
        if result.ok:
            return image, result
        raise RuntimeError(f"验证码连续识别失败：{result.error or '未知原因'}")

    def _submit(self, page):
        messages: List[str] = []

        def on_response(resp):
            if "/api" not in resp.url and "login" not in resp.url.lower():
                return
            try:
                data = resp.json()
            except Exception:
                return
            if isinstance(data, dict):
                for key in ("msg", "message", "errMsg", "error"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        messages.append(val.strip())

        page.on("response", on_response)
        try:
            try:
                page.click(SELECTORS["login_btn"], timeout=8000)
            except PWError:
                # 小窗口时页脚/遮罩会挡住登录按钮，滚动 + 强制点击
                self.log("warn", "登录按钮被遮挡，滚动后强制点击")
                try:
                    page.locator(SELECTORS["login_btn"]).first.scroll_into_view_if_needed(timeout=3000)
                except PWError:
                    pass
                page.click(SELECTORS["login_btn"], force=True, timeout=8000)
            try:
                page.wait_for_url(lambda u: "/login" not in u, timeout=12000)
                return "success", ""
            except PWTimeout:
                page.wait_for_timeout(600)
                ui_msgs = page.evaluate(
                    "() => [...document.querySelectorAll('.el-message, .el-message-box__message, .el-notification__content')]"
                    ".map(e => (e.innerText || '').trim()).filter(Boolean)"
                )
                joined = " | ".join(dict.fromkeys(messages + list(ui_msgs or [])))[:300]
                if not joined:
                    return "retry", "无提示信息（可能验证码错误）"
                if "成功" in joined:
                    return "success", joined
                if "验证码" in joined or "校验" in joined:
                    return "retry", joined
                return "fail", joined
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    # ---------- 逐个批阅 ----------
    def _pending_count(self, page) -> int:
        try:
            text = page.inner_text("body")
        except PWError:
            return 0
        m = re.search(r"待批阅\s*[(（](\d+)[)）]", text or "")
        return int(m.group(1)) if m else 0

    def _select_next_pending(self, page) -> str:
        """在左侧学生列表点开第一个「提交待批阅」条目，返回学生名（找不到返回空串）。"""
        js = """() => {
            const items = [...document.querySelectorAll('div')].filter(e => {
                const t = e.innerText || '';
                if (!/提交待批阅/.test(t) || t.length > 80) return false;
                const r = e.getBoundingClientRect();
                return r.width > 40 && r.height > 18;
            });
            if (!items.length) return null;
            items.sort((a, b) => a.getBoundingClientRect().height - b.getBoundingClientRect().height);
            const el = items[0];
            el.scrollIntoView({block: 'center'});
            const r = el.getBoundingClientRect();
            const first = (el.innerText || '').trim().split('\\n')[0].trim();
            return {x: r.x + r.width / 2, y: r.y + r.height / 2, name: first};
        }"""
        try:
            info = page.evaluate(js)
        except PWError:
            return ""
        if not info:
            return ""
        page.mouse.click(info["x"], info["y"])
        page.wait_for_timeout(1200)
        return str(info.get("name") or "").strip()

    def _click_visible(self, page, text: str, timeout: int = 8000) -> bool:
        loc = page.locator("button").filter(has_text=text)
        deadline = time.time() + timeout / 1000
        blocked = None
        while time.time() < deadline:
            for index in range(min(loc.count(), 10)):
                el = loc.nth(index)
                try:
                    if not (el.is_visible() and el.is_enabled()):
                        continue
                    el.scroll_into_view_if_needed(timeout=2000)
                    el.click(timeout=4000)
                    return True
                except PWError:
                    blocked = el
                    continue
            page.wait_for_timeout(300)
        if blocked is not None:
            # 按钮被其他元素挡住时（窄窗口常见），强制点击一次
            try:
                blocked.click(force=True, timeout=4000)
                self.log("warn", f"「{text}」被遮挡，已强制点击")
                return True
            except PWError:
                pass
        return False

    def _approve_one(self, page, score: str, comment: str) -> str:
        """审批当前选中的这一篇，成功返回空串，失败返回原因。"""
        if not self._click_visible(page, "通过审批", timeout=8000):
            return "找不到可见的「通过审批」按钮"

        dialog = page.locator(".el-dialog:visible").first
        try:
            dialog.wait_for(state="visible", timeout=10000)
        except PWTimeout:
            return "审批弹窗没有出现"

        try:
            title = (dialog.locator(".el-dialog__title").first.inner_text(timeout=2000) or "").strip()
        except PWError:
            title = ""
        if title and "审批" not in title:
            self.log("warn", f"弹窗标题异常：{title}")

        score_input = dialog.locator("input.el-input__inner").first
        score_input.fill(str(score))
        textarea = dialog.locator("textarea").first
        textarea.fill(str(comment))

        submit = dialog.locator("button").filter(has_text="确定提交").first
        if submit.count() == 0:
            submit = dialog.locator("button.el-button--primary").last
        submit.click(timeout=8000)

        # 可能再弹一层确认框
        page.wait_for_timeout(600)
        box = page.locator(".el-message-box:visible")
        if box.count():
            self.log("info", "出现二次确认框，点确定")
            confirm = box.first.locator(".el-message-box__btns button.el-button--primary").first
            if confirm.count() == 0:
                confirm = box.first.locator(".el-message-box__btns button").last
            confirm.click(timeout=5000)

        try:
            dialog.wait_for(state="hidden", timeout=15000)
        except PWTimeout:
            return "提交后弹窗没有关闭"
        return ""

    def _approve_all(self, page, score: str, comment: str, delay_ms: int, limit: int,
                     overrides: Optional[Dict[str, str]] = None) -> list:
        """按待批阅数量逐篇审批，一篇一篇来，不并发。返回已批阅的学生名列表。"""
        overrides = overrides or {}
        approved: List[str] = []
        seen_stuck = 0
        if self._pending_count(page) <= 0:
            self.log("ok", "当前没有待批阅周志，跳过批阅")
            return approved
        while len(approved) < limit:
            self._check_stop()
            pending = self._pending_count(page)
            if pending <= 0:
                break
            self.log("info", f"队列剩余 {pending} 篇，开始处理第 {len(approved) + 1} 篇")
            name = self._select_next_pending(page)
            if name:
                self.log("info", f"打开学生：{name}")
            use_score, hit = self._resolve_score(name, overrides, score)
            if hit:
                self.log("info", f"「{name}」命中指定分数：{use_score}")
            err = self._approve_one(page, use_score, comment)
            if err:
                self.log("err", f"「{name or '当前学生'}」批阅失败：{err}")
                break

            ok = False
            for _ in range(30):
                page.wait_for_timeout(500)
                if self._pending_count(page) < pending:
                    ok = True
                    break
            if not ok:
                seen_stuck += 1
                self.log("warn", f"待批阅数量仍是 {pending}，可能未生效")
                if seen_stuck >= 2:
                    break
            else:
                seen_stuck = 0
                approved.append(name or f"第{len(approved) + 1}篇")
                self.log(
                    "ok",
                    f"已通过：{name or '当前学生'}（分数 {use_score}"
                    f"{'（指定）' if hit else ''}，评语「{comment}」）",
                )
            if delay_ms > 0:
                page.wait_for_timeout(delay_ms)
        return approved

    @staticmethod
    def _normalize_name(text: str) -> str:
        return re.sub(r"\s+", "", text or "")

    @staticmethod
    def parse_score_overrides(text: str) -> Dict[str, str]:
        """解析「姓名=分数」多行文本，支持 = : ： , 、 空格 分隔，一行一条。"""
        overrides: Dict[str, str] = {}
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = SCORE_OVERRIDE_RE.match(line)
            if not m:
                continue
            name = re.sub(r"\s+", "", m.group(1))
            if name:
                overrides[name] = m.group(2)
        return overrides

    def _resolve_score(self, name: str, overrides: Dict[str, str], default: str):
        """命中指定姓名就返回指定分数，否则返回默认分数。返回 (分数, 是否命中)。"""
        clean = self._normalize_name(name)
        if clean and overrides:
            # 先精确匹配，再退回包含匹配（应对「张三（组长）」这类带后缀的名字）
            for key, value in overrides.items():
                if key == clean:
                    return value, True
            for key, value in overrides.items():
                if key and key in clean:
                    return value, True
        return default, False

    # ---------- 实习过程 -> 周日志批阅 -> 周志 ----------
    def _open_weekly_log(self, page) -> str:
        """顶部「实习过程」-> 左侧「周日志批阅」-> 页内「周志」，返回待批阅数量说明。"""
        self._check_stop()
        self._step(7)
        self._click_text(page, "实习过程", ("li.el-menu-item", ".el-menu-item", "a", "span", "div"))
        self.log("ok", "已点击顶部导航：实习过程")

        try:
            page.wait_for_selector(
                "li.el-menu-item:has-text('周日志批阅')", timeout=15000
            )
        except PWTimeout:
            raise RuntimeError("左侧侧边栏没有出现（找不到「周日志批阅」）")
        self.log("ok", "左侧侧边栏已出现")
        self._check_stop()

        self._step(8)
        self._click_text(page, "周日志批阅", ("li.el-menu-item", ".el-menu-item", "a", "span", "div"))
        try:
            page.wait_for_function(
                "() => location.href.includes('/internship/process')", timeout=15000
            )
        except PWTimeout:
            body = self._main_text(page)
            if "周日志批阅" not in body:
                raise RuntimeError("进入周日志批阅失败，页面没有切换")
        page.wait_for_timeout(1500)
        self.log("ok", f"已进入周日志批阅：{page.url}")
        self._check_stop()

        self._step(9)
        before = self._main_text(page)
        self._click_text(page, "周志", ("button", ".el-tabs__item", "span"))
        changed = False
        for _ in range(24):
            page.wait_for_timeout(500)
            if self._main_text(page) != before:
                changed = True
                break
        text = self._main_text(page)
        if not changed:
            self.log("warn", "点「周志」后内容没有明显变化（可能本来就是周志视图）")
        m = re.search(r"待批阅\s*[(（](\d+)[)）]", text)
        pending = m.group(1) if m else "?"
        self.log("ok", f"已切到周志视图，待批阅 {pending} 篇")
        if text:
            self.log("info", f"列表摘要：{text[:120]}")

        return f"（待批阅 {pending} 篇）"

    def _click_text(self, page, text: str, selectors):
        """按选择器优先级点击文字完全匹配的可见元素。"""
        pattern = re.compile(rf"^\s*{re.escape(text)}\s*$")
        tried = []
        for sel in selectors:
            try:
                loc = page.locator(sel).filter(has_text=pattern)
                count = min(loc.count(), 8)
            except PWError:
                continue
            for index in range(count):
                el = loc.nth(index)
                try:
                    if not el.is_visible():
                        continue
                    el.scroll_into_view_if_needed(timeout=3000)
                    el.click(timeout=8000)
                    return sel
                except PWError as exc:
                    tried.append(f"{sel}#{index}:{type(exc).__name__}")
                    continue
        # 兜底：窗口偏窄时站点可能把导航项折叠进「更多菜单」
        if self._click_inside_more_menu(page, text):
            self.log("warn", f"「{text}」被折叠进更多菜单，已从下拉中点击")
            return "submenu"
        self._dump_candidates(page)
        raise RuntimeError(f"没能点击「{text}」（尝试：{', '.join(tried) or '无匹配元素'}）")

    def _click_inside_more_menu(self, page, text: str) -> bool:
        """展开「更多菜单」类折叠项后再找目标文字。"""
        pattern = re.compile(rf"^\s*{re.escape(text)}\s*$")
        for title in ("更多菜单", "更多", "»", "..."):
            try:
                titles = page.locator(".el-submenu__title, .el-menu__submenu-title").filter(has_text=title)
                count = min(titles.count(), 3)
            except PWError:
                continue
            for index in range(count):
                trigger = titles.nth(index)
                try:
                    if not trigger.is_visible():
                        continue
                    trigger.click(timeout=4000)
                    page.wait_for_timeout(700)
                    items = page.locator("li, a, span, div").filter(has_text=pattern)
                    for i in range(min(items.count(), 8)):
                        item = items.nth(i)
                        if item.is_visible():
                            item.click(timeout=4000)
                            return True
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                except PWError:
                    continue
        return False

    def _main_text(self, page) -> str:
        js = """() => {
            const pane = [...document.querySelectorAll('.el-tab-pane')].find(p => p.offsetHeight > 100);
            const src = pane || document.querySelector('.app-main') || document.body;
            return (src.innerText || '').replace(/\\s+/g, ' ').trim();
        }"""
        try:
            return page.evaluate(js) or ""
        except PWError:
            return ""

    # ---------- 实习平台入口 ----------
    def _open_intern_page(self, page, text: str):
        """点击「实习平台」卡片 -> 确认弹窗点确定 -> 捕获 window.open 的新标签页并切过去。

        站点实现（chunk-4c9a86d0 handleJump）：
        卡片点击先弹 $modal.confirm，确定后写 localStorage 记录模块 id，
        再 window.open(origin + "/workbench", "_blank") 开新标签页。
        """
        ctx = page.context
        try:
            page.wait_for_function(
                "(t) => (document.body && document.body.innerText || '').includes(t)",
                arg=text,
                timeout=20000,
            )
        except PWTimeout:
            self._dump_candidates(page)
            raise RuntimeError(f"登录后页面上找不到「{text}」入口")

        target = self._locate_card(page, text)
        self._check_stop()
        before = list(ctx.pages)
        try:
            target.scroll_into_view_if_needed(timeout=4000)
        except PWError:
            pass
        try:
            target.click(timeout=8000)
        except PWError:
            self.log("warn", "卡片点击被拦截，改用强制点击")
            target.click(force=True, timeout=8000)
        self.log("info", f"已点击「{text}」卡片")

        # 可能出现 $modal.confirm 确认框（position:fixed，必须用 Playwright 的可见性判断）
        dialog = page.locator(".el-message-box:visible").first
        clicker = target
        try:
            dialog.wait_for(state="visible", timeout=5000)
            try:
                msg = (dialog.locator(".el-message-box__message").first.inner_text() or "").strip()
            except PWError:
                msg = ""
            self.log("info", f"站点弹出确认框：{msg or '（无文案）'}")
            confirm = dialog.locator(".el-message-box__btns button.el-button--primary").first
            if confirm.count() == 0:
                confirm = dialog.locator(".el-message-box__btns button").last
            clicker = confirm
        except PWTimeout:
            self.log("warn", "没有出现确认框，直接尝试捕获新标签页")

        new_page = None
        try:
            with ctx.expect_page(timeout=15000) as pinfo:
                clicker.click(timeout=8000)
            new_page = pinfo.value
        except PWError as exc:
            # 事件错过 / 老版本直接在卡片点击时开页 / 同标签页跳转
            extra = [p for p in ctx.pages if p not in before]
            if extra:
                new_page = extra[-1]
            else:
                self.log("warn", f"点确认后没等到新标签页（{type(exc).__name__}）")

        if new_page is None:
            # 退路：同标签页跳转也算成功（部分模块是 location.href）
            try:
                page.wait_for_function(
                    "(u) => location.href !== u", arg=before[0].url if before else page.url, timeout=8000
                )
            except PWTimeout:
                self._dump_candidates(page)
                raise RuntimeError("点确认后既没有新标签页也没有跳转")
            self.log("warn", "没有新标签页，改为沿用当前标签页跳转")
            self.page = page
            return page.url, (page.title() or "")

        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=30000)
        except PWTimeout:
            pass
        for _ in range(48):
            if new_page.url and new_page.url != "about:blank":
                break
            new_page.wait_for_timeout(250)
        try:
            new_page.wait_for_load_state("load", timeout=15000)
        except PWTimeout:
            pass
        try:
            new_page.bring_to_front()
        except PWError:
            pass
        self.page = new_page
        title = ""
        try:
            title = new_page.title()
        except PWError:
            pass

        # 新标签页是整页重新初始化，等它真正渲染出内容再收工
        snippet = ""
        for _ in range(30):
            self._check_stop()
            try:
                snippet = (new_page.inner_text("body") or "").strip()
            except PWError:
                snippet = ""
            if len(snippet) > 20:
                break
            new_page.wait_for_timeout(500)
        if snippet:
            self.log("ok", f"新标签页已渲染：{snippet.splitlines()[0][:40]}")
        return new_page.url, title

    def _locate_card(self, page, text: str):
        """优先找控制台的模块卡片（.routeList），再退回通用文字定位。"""
        card = page.locator(".routeList").filter(has_text=text).first
        try:
            if card.count() > 0 and card.is_visible():
                return card
        except PWError:
            pass
        return self._locate_nav(page, text)

    def _locate_nav(self, page, text: str):
        """在可见元素里找最贴合文字的、可点击的那一个（面积最小 = 最内层）。"""
        candidates = []
        for tag in ("a", "button", "li", "div", "span"):
            loc = page.locator(tag, has_text=text)
            try:
                count = min(loc.count(), 10)
            except PWError:
                continue
            for index in range(count):
                el = loc.nth(index)
                try:
                    if not el.is_visible():
                        continue
                    box = el.bounding_box() or {}
                    area = float(box.get("width", 0)) * float(box.get("height", 0))
                    label = (el.inner_text(timeout=1500) or "").strip()
                    exact = 0 if label == text else 1
                    candidates.append((exact, area, el))
                except PWError:
                    continue
        if not candidates:
            self._dump_candidates(page)
            raise RuntimeError(f"没有可点击的「{text}」元素")
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def _dump_candidates(self, page) -> None:
        js = """() => [...document.querySelectorAll('a,li,div,span,button')]
            .filter(e => e.offsetParent !== null && ((e.innerText||'').includes('实习') || (e.innerText||'').includes('平台')))
            .slice(0, 12)
            .map(e => ({tag: e.tagName.toLowerCase(), cls: String(e.className||'').slice(0,40),
                        text: (e.innerText||'').trim().slice(0,30), href: e.getAttribute('href')||''}))"""
        try:
            for item in page.evaluate(js):
                self.log("warn", f"页面候选元素：{item}")
        except Exception:
            pass

    # ---------- 单位列表抓取 ----------
    @staticmethod
    def fetch_units(url: str) -> List[str]:
        with sync_playwright() as pw:
            browser, _ = LoginRunner._launch(pw, True, 0)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_selector(SELECTORS["unit"], timeout=25000)
                page.locator(SELECTORS["unit"]).first.click()
                page.wait_for_timeout(1200)
                units = page.evaluate(
                    "() => [...document.querySelectorAll('.el-select-dropdown li')]"
                    ".map(li => (li.innerText || '').trim()).filter(Boolean)"
                )
                return list(dict.fromkeys(units))
            finally:
                browser.close()
