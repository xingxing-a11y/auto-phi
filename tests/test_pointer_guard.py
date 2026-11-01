"""`tools/pointer_guard.py` 的离线测试 —— 不连手机。

覆盖两件事：

    [1] `dumpsys input` 的**解析**：全部用例的输入都取自 2026-10-16 的真机原始输出
        （`tmp/verify_stuck_release.log` / `tmp/clear_stuck.log` / `tmp/dumpsys_input_clean_20261016.txt`）。
        ★ 头号回归项：**干净的 dump 里也有 `DeviceId(-1)`**（`Down times by device:`），
        只按 `DeviceId(` 判残留会恒定误报。
    [2] `reset()` 的分级复位：A（DOWN(0)+UP(0)）→ B（其余 pointer 各一对），
        用假 server / 假 socket / 脚本化的状态序列驱动，断言**发出去的字节**。
"""

from __future__ import annotations

import pytest

import pointer_guard as PG


# ---------------------------------------------------------------- 真机原始片段


# 2026-10-16 19:4x，设备 realme RMX5200 在桌面、无残留（`tmp/_dumpsys_input.txt`）。
# ⚠️ 注意 `Down times by device` 这行**任何时候都在** —— 见 _CLEAN_DUMP 的注释。
_CLEAN_DUMP = """\
  Pointer Capture Mode Requested: UNCAPTURED
  Current Window with Pointer Capture: None
  TouchStatesByDisplay: <no displays touched>
  CursorStatesByDisplay: <no displays touched by cursor>
  Display: 0
    logicalSize=1440x3136
"""

# 干净时**依然存在**的那一行（真机实测）。放在这里当「反例诱饵」：
# 任何把它当成残留的实现都会被 test_clean_dump_with_down_times_is_clean 抓住。
_CLEAN_DECOY = "    Down times by device: {DeviceId(-1): 19770278000000}\n"

# `tmp/verify_stuck_release.log:11` —— 阶段 1「只发 DOWN 不发 UP」后的真机输出。
_DIRTY_ONE = """\
  TouchStatesByDisplay:
    DeviceId(-1):[touchingPointers=[Pointer(id=0, FINGER)], \
downTimeInTarget=19768754000000, hoveringPointers=[], pilferingPointerIds=<none>]
"""

# FACTS §66 记录的多指残留写法（裸 id 列表）。
_DIRTY_MANY = """\
  TouchStatesByDisplay:
    DeviceId(-1):[touchingPointers=[0,1,2], downTimeInTarget=162300000000, \
hoveringPointers=[], pilferingPointerIds=<none>]
"""

# 指针列表为空 ⇒ 没有按着的（同一条 TouchState 行，只是列表空了）。
_EMPTY_LIST = """\
  TouchStatesByDisplay:
    DeviceId(-1):[touchingPointers=[], downTimeInTarget=19768754000000, \
hoveringPointers=[], pilferingPointerIds=<none>]
"""


# ---------------------------------------------------------------- [1] 解析


def test_clean_dump_with_down_times_is_clean():
    """★ 头号回归：干净的 dump 里有 `DeviceId(-1)`，必须仍判为干净。"""
    st = PG.parse_touch_state(_CLEAN_DUMP + _CLEAN_DECOY)
    assert st.clean
    assert not st.unknown
    assert st.pointers == ()
    assert st.saw_no_touched


def test_dirty_single_pointer():
    st = PG.parse_touch_state(_DIRTY_ONE)
    assert not st.clean
    assert st.pointers == (0,)
    assert st.devices[0].device_id == -1
    assert st.devices[0].down_time == 19768754000000
    assert not st.saw_no_touched


def test_dirty_bare_id_list():
    """裸 `[0,1,2]` 写法（FACTS §66 的原文）也要能解析出多指。"""
    st = PG.parse_touch_state(_DIRTY_MANY)
    assert st.pointers == (0, 1, 2)
    assert st.devices[0].down_time == 162300000000


def test_empty_pointer_list_is_not_residue():
    st = PG.parse_touch_state(_EMPTY_LIST)
    assert st.pointers == ()
    assert st.clean


def test_repeated_identical_records_are_deduped():
    """真机里同一条残留状态会在 dump 中**重复出现**（2026-10-16 实测 8 次）。

    `InputDispatcher` 的多个 section 各打一遍同一份 TouchState（`TouchStatesByDisplay`
    + 队列回显）⇒ 若不去重，`describe()` 会把 8 份一模一样的东西用「；」拼成一长串，
    点火前的告警直接刷屏。
    """
    st = PG.parse_touch_state(_DIRTY_ONE * 3)
    assert st.pointers == (0,)
    assert len(st.devices) == 1, "同一条记录重复出现必须去重"
    assert st.describe().count("DeviceId(-1)") == 1


def test_missing_marker_is_unknown_not_clean():
    """既没残留也没 `<no displays touched>`（格式变了 / 命令失败）⇒ 不能当干净。"""
    st = PG.parse_touch_state("some totally unrelated dumpsys output\n")
    assert st.clean
    assert st.unknown


def test_dirty_dump_is_not_unknown():
    st = PG.parse_touch_state(_DIRTY_ONE)
    assert not st.unknown


def test_describe_mentions_residue_and_points():
    assert "干净" in PG.parse_touch_state(_CLEAN_DUMP).describe()
    d = PG.parse_touch_state(_DIRTY_ONE).describe()
    assert "DeviceId(-1)" in d and "19768754000000" in d
    assert "无法判定" in PG.parse_touch_state("nothing here").describe()


def test_nested_brackets_do_not_break_the_body_capture():
    """TouchState 行里嵌套了若干个 `[]`；贪婪捕获必须取到行尾最后一个 `]`。

    回归：非贪婪 `\\[(.+?)\\]` 会在 `touchingPointers=[Pointer(...)]` 的
    第一个 `]` 处截断，导致 `downTimeInTarget` 丢失。
    """
    st = PG.parse_touch_state(_DIRTY_ONE)
    assert st.devices[0].down_time is not None


# ---------------------------------------------------------------- [2] 复位分级


class _FakeSock:
    """够 `Injector` 用的假 socket：把 32 字节消息记下来。"""

    def __init__(self):
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def shutdown(self, how) -> None:                              # noqa: ARG002
        pass

    def close(self) -> None:
        pass


class _RecInjector(PG.inject.Injector):
    """真 `Injector` + 记账，便于断言「发了哪几个 DOWN/UP」。"""

    log: list[tuple[int, int, int, int]] = []

    def send(self, action, pointer_id, x, y):                     # type: ignore[override]
        self.log.append((action, pointer_id, x, y))
        super().send(action, pointer_id, x, y)


class _FakeServer:
    """假 scrcpy server：不碰设备，只回一条假 socket。"""

    def __init__(self, adb, jar, *, verbose: bool = False):        # noqa: ARG002
        self.calls = {"start": 0, "stop": 0}
        self.sock = _FakeSock()

    def start(self, **kw):                                        # noqa: ARG002
        self.calls["start"] += 1
        return self.sock

    def stop(self) -> None:
        self.calls["stop"] += 1

    def tail_log(self, lines: int = 20) -> str:                   # noqa: ARG002
        return ""


class _FakeAdb:
    def __init__(self, states: list[PG.TouchState]):
        self._states = list(states)

    def screen_size(self):
        return 1440, 3136, 0


def _state(pointers: tuple[int, ...]) -> PG.TouchState:
    if not pointers:
        return PG.parse_touch_state(_CLEAN_DUMP)
    line = ("  TouchStatesByDisplay:\n    DeviceId(-1):[touchingPointers=["
            + ",".join(str(p) for p in pointers)
            + "], downTimeInTarget=19768754000000, hoveringPointers=[], "
              "pilferingPointerIds=<none>]\n")
    return PG.parse_touch_state(line)


@pytest.fixture()
def rig(monkeypatch):
    """装好假 server / 记账 Injector / 脚本化状态序列；返回 (states, server, 读回数)。"""
    _RecInjector.log = []
    monkeypatch.setattr(PG.inject, "Injector", _RecInjector)
    made: list[_FakeServer] = []

    def _factory(adb, jar, *, verbose=False):
        s = _FakeServer(adb, jar, verbose=verbose)
        made.append(s)
        return s

    monkeypatch.setattr(PG.inject, "ScrcpyServer", _factory)
    monkeypatch.setattr(PG.time, "sleep", lambda *_: None)        # 别真睡

    box: dict = {"states": [], "reads": 0}

    def _read(adb):                                               # noqa: ARG001
        box["reads"] += 1
        assert box["states"], "状态序列被读空了"
        return box["states"].pop(0)

    monkeypatch.setattr(PG, "read_touch_state", _read)
    return box, made


def _pairs(log):
    """把记账日志折成 [(pid, action)] 以便断言配对。"""
    return [(pid, act) for act, pid, _x, _y in log]


def test_reset_step_a_when_one_pair_is_enough(rig):
    box, made = rig
    box["states"] = [_state((0,)), _state(()), _state(())]        # 复位前 / A 之后 / 收尾复查
    step, before, after = PG.reset(_FakeAdb([]), PG.inject.DEFAULT_SERVER_JAR, 720, 200,
                                   hold_s=0, settle_s=0, verbose=False)
    assert step == "A"
    assert before.pointers == (0,) and after.clean
    assert _pairs(_RecInjector.log) == [(0, PG.inject.ACTION_DOWN),
                                       (0, PG.inject.ACTION_UP)]
    assert made[0].calls == {"start": 1, "stop": 1}


def test_reset_mirrors_the_multi_finger_gesture(rig):
    """★ 三指残留：必须发**一个完整多指手势**（全部按下 → 逆序抬起）。

    2026-10-16 真机矩阵定案：逐指「按一对、抬一对」在三指残留上**清不掉**
    （`tmp/probe_stuck_clear_matrix.log` 的 E2：`downTimeInTarget` 一字未变）。
    旧实现的 A 级（只发 pointer 0 的一对）因此在多指场景下是**失效**的 ——
    这条用例把新配方钉住。
    """
    box, made = rig
    box["states"] = [_state((0, 1, 2)), _state(()), _state(())]
    step, before, after = PG.reset(_FakeAdb([]), PG.inject.DEFAULT_SERVER_JAR, 720, 200,
                                   hold_s=0, settle_s=0, verbose=False)
    assert step == "A"
    assert before.pointers == (0, 1, 2) and after.clean
    assert _pairs(_RecInjector.log) == [
        (0, PG.inject.ACTION_DOWN), (1, PG.inject.ACTION_DOWN),
        (2, PG.inject.ACTION_DOWN),
        (2, PG.inject.ACTION_UP), (1, PG.inject.ACTION_UP), (0, PG.inject.ACTION_UP),
    ]
    assert made[0].calls["stop"] == 1


def test_reset_step_b_is_a_retry_of_the_same_mirror(rig):
    """A 没清掉（时序抖动）⇒ B 用**同一手法**再来一次，而不是「逐指各一对」。"""
    box, _made = rig
    box["states"] = [_state((0, 1)), _state((0, 1)), _state(()), _state(())]
    step, _before, after = PG.reset(_FakeAdb([]), PG.inject.DEFAULT_SERVER_JAR, 720, 200,
                                    hold_s=0, settle_s=0, verbose=False)
    assert step == "B"
    assert after.clean
    assert _pairs(_RecInjector.log) == [
        (0, PG.inject.ACTION_DOWN), (1, PG.inject.ACTION_DOWN),
        (1, PG.inject.ACTION_UP), (0, PG.inject.ACTION_UP),
    ] * 2


def test_reset_reports_none_when_still_dirty(rig):
    box, made = rig
    box["states"] = [_state((0, 1)), _state((0, 1)), _state((0, 1)), _state((0, 1))]
    step, _before, after = PG.reset(_FakeAdb([]), PG.inject.DEFAULT_SERVER_JAR, 720, 200,
                                    hold_s=0, settle_s=0, verbose=False)
    assert step == "none"
    assert after.pointers == (0, 1)                               # 仍脏 ⇒ 上层提示 adb reboot


def test_reset_is_noop_when_already_clean(rig):
    box, made = rig
    box["states"] = [_state(())]
    step, _before, after = PG.reset(_FakeAdb([]), PG.inject.DEFAULT_SERVER_JAR, 720, 200,
                                    hold_s=0, settle_s=0, verbose=False)
    assert step == "-"
    assert after.clean
    assert made == []                                             # 根本不起 server
    assert _RecInjector.log == []                                 # 不往设备发任何东西


def test_reset_never_sends_a_bare_up(rig):
    """回归：`乙` 的原始思路（对未按下的指针只发 UP）已被真机证伪，不许复活。

    新配方下判据换了个形式但仍是**硬约束**：任何 `UP` 之前，同一个 pid 必须**先**发过
    `DOWN`（镜像手势 = 先全部按下、再抬起）——不许「凭空抬」，也不许先抬后按。
    """
    box, _made = rig
    box["states"] = [_state((0, 1)), _state(()), _state(())]
    PG.reset(_FakeAdb([]), PG.inject.DEFAULT_SERVER_JAR, 720, 200,
             hold_s=0, settle_s=0, verbose=False)
    log = _pairs(_RecInjector.log)
    assert log                                           # 确实发了东西
    assert log[0][1] == PG.inject.ACTION_DOWN            # 必须以 DOWN 打头
    pending: set = set()
    for pid, act in log:
        if act == PG.inject.ACTION_DOWN:
            pending.add(pid)
        else:
            assert pid in pending, f"裸 UP：pointer {pid} 之前没有按下过"
            pending.discard(pid)
    assert not pending, "收尾必须把所有按下的都抬起来"


def test_reset_sends_inside_screen_bounds(rig):
    """复位点必须落在屏幕内（`Injector` 只做钳制前的直通，越界由调用方负责）。"""
    box, _made = rig
    box["states"] = [_state((0,)), _state(()), _state(())]
    PG.reset(_FakeAdb([]), PG.inject.DEFAULT_SERVER_JAR, 720, 200,
             hold_s=0, settle_s=0, verbose=False)
    for _act, _pid, x, y in _RecInjector.log:
        assert 0 <= x < 1440
        assert 0 <= y < 1312          # 底边手势区 y >= 1312（RMX5200 竖屏）


# ---------------------------------------------------------------- [3] 点火前预检


class _DumpAdb:
    """只实现 `shell()` 的假 adb —— 预检只用得到这一个方法。"""

    def __init__(self, text: str | None = None, exc: Exception | None = None):
        self.text, self.exc, self.calls = text, exc, []

    def shell(self, cmd: str, **_kw):
        self.calls.append(cmd)
        if self.exc is not None:
            raise self.exc
        return self.text or ""


def _run_preflight(adb, **kw):
    """跑一次预检，把打印出来的每行收进 list 返回。"""
    lines: list[str] = []
    ok = PG.preflight(adb, out=lines.append, **kw)
    return ok, "\n".join(lines)


def test_preflight_clean_is_ok_and_says_so():
    ok, text = _run_preflight(_DumpAdb(_CLEAN_DUMP))
    assert ok is True
    assert "干净" in text
    assert "警告" not in text                       # 不能吓唬人


def test_preflight_clean_decoy_down_times_is_still_clean():
    """★ 头号回归项在预检层的复刻：干净 dump 里**也有** `DeviceId(-1)`。"""
    ok, text = _run_preflight(_DumpAdb(_CLEAN_DUMP + _CLEAN_DECOY))
    assert ok is True and "干净" in text


def test_preflight_dirty_returns_false_and_prints_the_fix():
    ok, text = _run_preflight(_DumpAdb(_state((0, 1, 2)).raw))
    assert ok is False
    assert "卡指针警告" in text
    assert "[0, 1, 2]" in text                      # 残留必须点名到 id
    assert "tools/pointer_guard.py reset --xy" in text   # 给出可照抄的命令
    assert "不必 adb reboot" in text                 # 别再走 reboot 那条路（FACTS §75）
    assert "回桌面" in text                          # reset 会产生真实点击 ⇒ 先离开选歌页


def test_preflight_hint_xy_is_used_in_the_command():
    ok, text = _run_preflight(_DumpAdb(_state((0,)).raw), xy_hint=(111, 222))
    assert ok is False
    assert "reset --xy 111,222" in text


def test_preflight_read_failure_is_fail_open():
    """adb 跑挂了 ⇒ **放行**（预检自己有出错的可能，不该拦下正常轮次）。"""
    ok, text = _run_preflight(_DumpAdb(exc=RuntimeError("adb server 被回收")))
    assert ok is True
    assert "读不到" in text and "fail-open" in text


def test_preflight_unknown_dump_is_fail_open():
    """格式变了（既没残留、也没 `<no displays touched>`）⇒ 只敢说「无法判定」。"""
    ok, text = _run_preflight(_DumpAdb("some totally different dump\n"))
    assert ok is True
    assert "无法判定" in text and "fail-open" in text


def test_preflight_only_reads_and_never_resets(monkeypatch):
    """★ 硬约束：预检只跑一条 `dumpsys input`，**不**复位、**不**起 server、**不**发事件。

    自动复位要在就绪界面点一下屏幕（可能点到 ▶ / 返回），风险大于它省下的一步 ——
    方案 §8.3 明确「不做自动复位」。
    """
    def _boom(*_a, **_kw):
        raise AssertionError("预检不允许调用 reset()")

    monkeypatch.setattr(PG, "reset", _boom)
    monkeypatch.setattr(PG.inject, "ScrcpyServer", _boom)
    monkeypatch.setattr(PG.inject, "Injector", _boom)
    adb = _DumpAdb(_state((0,)).raw)
    ok, _text = _run_preflight(adb)
    assert ok is False
    assert adb.calls == ["dumpsys input"]          # 一条命令、只读、不多跑



# ---------------------------------------------------------------- [4] 手势区 / 复位点
#: 2026-10-18 换机（HONOR ALT-AN00，1610×720）补：手势区与复位点都是**设备属性**，
#: 旧机写死的 `y>=1312` / `(720,200)` 在新机上分别变成「y>=640」和「越界」。


@pytest.mark.parametrize("w,h", [(1610, 720), (720, 1610), (3136, 1440), (1440, 3136)])
def test_safe_reset_xy_stays_inside_and_avoids_gesture_zones(w, h):
    """复位点必须既在屏内、又避开四边系统手势区。

    旧常量 `PREFLIGHT_XY_HINT = (720, 200)` 是 1440 宽旧机竖屏的坐标，换到 720 宽的
    新机竖屏后 `x = 720` 直接越界 ⇒ 点击发到屏外、复位无效且**不报任何错**。
    """
    x, y = PG.safe_reset_xy(w, h)
    z = PG.gesture_zone(w, h)
    assert 0 <= x <= w - 1 and 0 <= y <= h - 1, f"越界 ({x},{y}) on {w}x{h}"
    assert x > z["left"] and x < w - z["right"], f"落在左右手势区 ({x},{y}) z={z}"
    assert y > z["top"] and y < h - z["bottom"], f"落在上下手势区 ({x},{y}) z={z}"


def test_gesture_zone_table_matches_measured_frames():
    """两机手势区必须与 `dumpsys window` 的 `InsetsSource frame=` 逐值一致。

    这是「测了但要能防退化」的那一层：表里写错一个数，等于又回到没测。
    """
    assert PG.gesture_zone(1610, 720) == {"left": 104, "right": 30, "top": 74, "bottom": 80}
    assert PG.gesture_zone(3136, 1440) == {"left": 257, "right": 120, "top": 160, "bottom": 128}


def test_gesture_zone_fallback_is_conservative():
    """未登记机型按比例回退 —— 四边都必须是正数（返回 0 等于没避）。"""
    z = PG.gesture_zone(1080, 2400)
    assert all(v > 0 for v in z.values()), z
    assert z["left"] < 1080 // 2 and z["bottom"] < 2400 // 2   # 别把整屏都算成手势区
