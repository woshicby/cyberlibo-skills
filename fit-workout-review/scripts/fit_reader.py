#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fit_reader.py — 本地 FIT 活动读取器（fit-workout-review skill 数据层）

把本地 FIT 文件夹里的活动按"最小数据梯度"读取为结构化 JSON，
语义对齐 coros-workout-review 的数据读取梯度：
  list   = 活动列表（只给摘要，不含位置/内部 ID）
  summary= 活动详情（含分圈，默认；含数据质量标记）
  laps   = 分圈/分段
  window = 自定义时间窗（用户明确问"最后 N 分钟"时用）

隐私边界（与 references/privacy-safety.md 一致）：
  - 永不输出 GPS 坐标、起终点、路线（含 semicircle 原始值）
  - 内部标识只用文件名（用户本地文件名），不输出设备 ID/序列号
  - 输出目录路径默认不回显 --dir 之外的绝对路径细节

FIT 文件夹位置按优先级解析：
  1. --dir 参数
  2. 环境变量 FIT_WORKOUT_REVIEW_FIT_DIR
  3. 默认: 用户主目录下的 FIT 文件夹（可被上游覆盖）

用法示例：
  python fit_reader.py list --days 14
  python fit_reader.py list --days 30 --sport running --limit 5
  python fit_reader.py summary --file "20261005-...-647281649.fit"
  python fit_reader.py laps --file "....fit"
  python fit_reader.py window --file "....fit" --last-minutes 20 --sample 30

退出码：0 成功；2 配置/参数错误；3 解析失败；4 找不到文件。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# garmin_fit_sdk 加载：优先当前 Python 环境（venv / pip install garmin-fit-sdk）。
# 未安装时给出明确安装指引（见 SKILL.md「环境准备」与 references/fit-data-sources.md）。
try:
    from garmin_fit_sdk import Decoder, Stream
except ImportError:
    raise SystemExit(
        "缺少 garmin-fit-sdk。请先执行：\n"
        "  python -m venv <skill>/.venv-windows   # macOS 用 .venv-macos\n"
        "  <venv>/python -m pip install garmin-fit-sdk\n"
        "或 pip install -r <skill>/scripts/requirements.txt")

FIT_EPOCH = datetime(1989, 12, 31)
DEFAULT_TZ_OFFSET_SECONDS = 8 * 3600  # 文件无时区信息时按 UTC+8

# 文件名约定: YYYYMMDD-HHMMSS-sport[-label]-...fit（与用户 FIT 文件夹一致）
FILENAME_PAT = re.compile(
    r"^(?P<date>\d{8})-(?P<time>\d{6})-(?P<sport>[a-z_]+)"
)

SPORT_NORMALIZE = {
    "running": "running", "run": "running", "trail_running": "trail_running",
    "walking": "walking", "hiking": "hiking", "cycling": "cycling",
    "biking": "cycling", "strength_training": "strength",
    "swimming": "swimming", "rowing": "rowing",
}

MISSING = "missing"      # 字段不存在或全空
PARTIAL = "partial"      # 部分记录有值
OK = "ok"


# ---------------- 基础工具 ----------------

def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    return None


def _round(v: Optional[float], nd: int = 2) -> Optional[float]:
    return None if v is None else round(v, nd)


def _s2pace_ms(mps: Optional[float]) -> Optional[float]:
    """m/s -> 秒/公里"""
    if mps is None or mps <= 0:
        return None
    return 1000.0 / mps


def _pace_display(s_per_km_vals: List[Optional[float]]) -> Optional[str]:
    """s/km 值列表 -> 'm:ss' 记法，区间 -> 'm:ss-m:ss'（快端在前）。

    例: [265, 276] -> '4:25-4:36'，与常见文件名命名习惯一致。
    """
    def one(v: Optional[float]) -> Optional[str]:
        if v is None or v <= 0:
            return None
        m, s = divmod(int(round(v)), 60)
        return f"{m}:{s:02d}"

    parts = [one(v) for v in s_per_km_vals]
    parts = [p for p in parts if p]
    return "-".join(parts) if parts else None


def _mean(vals: List[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _min(vals: List[Optional[float]]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def _max(vals: List[Optional[float]]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def _coverage(records: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    """字段覆盖质量: missing / partial / ok"""
    present = [r[key] for r in records if _num(r.get(key)) is not None]
    if not records:
        return {"status": MISSING, "coverage": 0.0}
    frac = len(present) / len(records)
    if frac == 0:
        return {"status": MISSING, "coverage": 0.0}
    if frac < 0.95:
        return {"status": PARTIAL, "coverage": round(frac, 3)}
    return {"status": OK, "coverage": round(frac, 3)}


def _local_start_time(msgs: Dict[str, Any]) -> Optional[str]:
    """从 activity/session 提取本地开始时间（ISO, 带时区偏移）。"""
    tz = DEFAULT_TZ_OFFSET_SECONDS
    for act in msgs.get("activity_mesgs", []):
        ts = act.get("timestamp")
        lts = act.get("local_timestamp")
        if ts is not None and lts is not None:
            tz = int(lts) - int(ts)
            break
    raw: Optional[int] = None
    for sess in msgs.get("session_mesgs", []):
        if sess.get("start_time") is not None:
            raw = int(sess["start_time"])
            break
    if raw is None:
        for fid in msgs.get("file_id_mesgs", []):
            if fid.get("time_created") is not None:
                raw = int(fid["time_created"])
                break
    if raw is None:
        return None
    utc = FIT_EPOCH + timedelta(seconds=raw)
    local = utc + timedelta(seconds=tz)
    return local.isoformat()


def _parse_args_sport(sport: str) -> str:
    return SPORT_NORMALIZE.get(sport.lower(), sport.lower())


def _resolve_dir(cli_dir: Optional[str]) -> str:
    d = cli_dir or os.environ.get("FIT_WORKOUT_REVIEW_FIT_DIR") \
        or os.path.join(os.path.expanduser("~"), "FIT")
    return os.path.abspath(os.path.expanduser(d))


def _list_fit_files(fit_dir: str) -> List[str]:
    if not os.path.isdir(fit_dir):
        return []
    return sorted(
        f for f in os.listdir(fit_dir)
        if f.lower().endswith(".fit") and not f.startswith(".")
    )


def _file_meta(name: str, path: str) -> Dict[str, Any]:
    """从文件名解析摘要（不打开文件，list 用）。"""
    m = FILENAME_PAT.match(name)
    out: Dict[str, Any] = {"file": name, "size_bytes": os.path.getsize(path)}
    if m:
        out["date"] = m.group("date")
        out["time"] = m.group("time")
        out["sport_in_name"] = m.group("sport")
    return out


# ---------------- 解析 ----------------

def _decode(path: str) -> Dict[str, Any]:
    with open(path, "rb") as fh:
        data = fh.read()
    msgs, errors = Decoder(Stream.from_byte_array(data)).read(
        convert_datetimes_to_dates=False,
        expand_sub_fields=True,
        expand_components=True,
        merge_heart_rates=False,
    )
    if errors:
        # 部分文件有坏记录，保留可解析部分
        pass
    return msgs


def _session_block(msgs: Dict[str, Any]) -> Dict[str, Any]:
    s = (msgs.get("session_mesgs") or [{}])[0]
    return {
        "sport": s.get("sport_profile_name") or s.get("sport"),
        "start_time": _local_start_time(msgs),
        "total_timer_time_s": _num(s.get("total_timer_time")),
        "total_distance_m": _num(s.get("total_distance")),
        "avg_heart_rate": _round(_num(s.get("avg_heart_rate")), 1),
        "max_heart_rate": _num(s.get("max_heart_rate")),
        "avg_power": _round(_num(s.get("avg_power")), 1),
        "max_power": _num(s.get("max_power")),
        "normalized_power": _round(_num(s.get("normalized_power")), 1),
        "avg_speed_mps": _round(_num(s.get("enhanced_avg_speed")), 3),
        "max_speed_mps": _round(_num(s.get("enhanced_max_speed")), 3),
        "total_ascent_m": _round(_num(s.get("total_ascent")), 1),
        "total_descent_m": _round(_num(s.get("total_descent")), 1),
        "total_calories": _num(s.get("total_calories")),
        "total_grit": _num(s.get("total_grit")),
        "avg_vertical_oscillation": _round(
            _num(s.get("avg_vertical_oscillation")), 2),
        "avg_stance_time": _round(_num(s.get("avg_stance_time")), 1),
        "avg_step_length": _round(_num(s.get("avg_step_length")), 3),
        "indoor": bool(s.get("indoor")),
    }


def _workout_block(msgs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """结构化训练段（workout steps）——间歇结构识别的第一证据。

    输出字段语义对齐 Garmin：
      intensity: active / rest（rest 段即组间恢复）
      duration_time: 该段目标时长（秒）
      custom_target_speed_low/high: m/s（换算成 pace_s_per_km 供人读）
      custom_target_heart_rate_low/high: 目标心率区间
      repeat_steps / duration_type: 重复结构（如 repeat_until_steps_cmplt + repeat_steps=3）
    """
    steps = []
    for st in msgs.get("workout_step_mesgs", []):
        tgt = st.get("target_type")
        speed_lo = _num(st.get("custom_target_speed_low"))
        speed_hi = _num(st.get("custom_target_speed_high"))
        hr_lo = _num(st.get("custom_target_heart_rate_low"))
        hr_hi = _num(st.get("custom_target_heart_rate_high"))
        step = {
            "index": st.get("message_index"),
            "intensity": st.get("intensity"),           # active / rest
            "target_type": tgt,
            "duration_type": st.get("duration_type"),
            "duration_time_s": _round(_num(st.get("duration_time")), 1),
            "repeat_steps": st.get("repeat_steps") or st.get("num_repetitions"),
        }
        if tgt == "speed" and (speed_lo or speed_hi):
            step["target_speed_mps"] = [speed_lo, speed_hi]
            pace_vals = [
                _round(_s2pace_ms(speed_hi), 1), _round(_s2pace_ms(speed_lo), 1)]
            step["target_pace_s_per_km"] = pace_vals
            step["target_pace_display"] = _pace_display(pace_vals)
        if tgt == "heart_rate" and (hr_lo or hr_hi):
            step["target_heart_rate"] = [hr_lo, hr_hi]
        steps.append(step)
    return steps


# ---------- 文件名 @ 标注解析与 workout_step 对齐 ----------
# 用户课表文件名习惯: <时长><N×><工作段>@<目标>[_恢复]，例:
#   10×800@430_3min        10 组 800m @ 配速 4:30, 恢复 3 分钟
#   40min@127-1596×10s_110s 40 分钟主段 @ 心率 127-159, 6 组 10s 快 / 110s 恢复
#   90min@530-540          90 分钟 @ 配速 5:30-5:40
#   1×10min@E              1 组 10 分钟 @ E 区间
#   25k@2.0-2.5            25k @ 每 10km 2:00-2:30（小数 = 小时目标）
#   1×1h@法特莱克           文字标注
# 数字判别核心规则（用户确认）: 1 分多/km 配速不现实, 所以 100-199 必为心率;
# 400-599 等高位按紧凑配速 mss 解析（436 -> 4:36 -> 276 s/km）。

def _classify_numeric(n: int) -> Dict[str, Any]:
    """把一个 @ 后整数分类: pace（紧凑 mss 配速）/ heart_rate / unknown。"""
    m, ss = divmod(int(n), 100)
    pace_valid = ss < 60 and m >= 1
    out: Dict[str, Any] = {"n": n}
    if 100 <= n <= 199:
        out.update(kind="heart_rate", confidence="high")
    elif n >= 200 and pace_valid:
        out.update(kind="pace", s_per_km=m * 60 + ss,
                   confidence="medium" if n < 300 else "high")
    elif 60 <= n < 100:
        out.update(kind="heart_rate", confidence="low")
    else:
        out.update(kind="unknown", confidence="none")
    return out


def _split_glued(x: int) -> Tuple[Optional[int], Optional[int]]:
    """4 位数字拆分: 前 3 位为值, 末位为粘连的重复次数/距离/分钟尾。

    例: 1596 -> (159, 6); 5022 -> (502, 2); 520 -> (520, None)
    """
    if 1000 <= x <= 9999:
        s = str(x)
        return int(s[:3]), int(s[3])
    return x, None


def _parse_target_token(raw: str) -> Dict[str, Any]:
    """解析单个 @ 后的目标 token（不含 @）。"""
    raw = raw.strip()
    # 文字/字母标注（E/M/R/easyM/法特莱克/轻松跑...）
    if not raw or not re.match(r"^[0-9]", raw):
        if re.match(r"^[A-Za-z]", raw):
            return {"kind": "zone", "label": re.split(r"[-_×(]", raw)[0]}
        return {"kind": "text", "value": re.split(r"[_×(]", raw)[0][:8]}
    # 小数: 心率分区（25k@2.0-2.5 = 心率区 2.0-2.5；bpm↔分区映射随
    # 静息/最大心率变化，不硬换算，只与设备段实测心率区间并列陈述）
    dm = re.match(r"^(\d+(?:\.\d+))[-](\d+(?:\.\d+))", raw)
    if dm and "." in raw:
        return {"kind": "hr_zone", "value": [
            float(dm.group(1)), float(dm.group(2))], "consumed": dm.end()}
    sm = re.match(r"^(\d+)(?:\.(\d+))?", raw)
    a = int(sm.group(1))
    a_dec = sm.group(2) is not None
    rest = raw[sm.end():]
    # 区间: 上界 1-4 位数字（4 位 = 3 位值 + 1 位粘连尾）
    if rest.startswith("-"):
        dm2 = re.match(r"^-(\d{1,4})(?!\d)", rest)
        if dm2:
            dseq = dm2.group(1)
            b_extra = None
            if len(dseq) <= 3:
                b = int(dseq)
            else:  # 4 位: 前 3 位是值, 末位粘连
                b, b_extra = _split_glued(int(dseq))
            a_cls, b_cls = _classify_numeric(a), _classify_numeric(b)
            if a_cls["kind"] != b_cls["kind"]:
                # 两端分类不一致（截断/粘连所致），取低置信并标注
                for c in (a_cls, b_cls):
                    c["confidence"] = "low"
            return {"kind": "range", "lo": a, "hi": b,
                    "lo_cls": a_cls, "hi_cls": b_cls,
                    "hi_extra": b_extra, "consumed": sm.end() + dm2.end()}
    # 单值（4 位 = 3 位值 + 1 位粘连尾；5+ 位 = 截断残留，不裁决）
    a_extra = None
    digits_len = len(sm.group(1))
    if digits_len >= 5:
        return {"kind": "unknown", "lo": None, "consumed": digits_len}
    if digits_len == 4:
        a, a_extra = _split_glued(a)
    return {"kind": "single", "lo": a, "lo_cls": _classify_numeric(a),
            "a_extra": a_extra, "consumed": digits_len}


_TIME_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(s|sec|mins|min|mim)\b")
_DIST_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(m|k)\b")


def _extract_work(pre: str) -> Optional[Dict[str, Any]]:
    """从 @ 前缀提取工作段: 取最后一个 × 之后的部分（无 × 取尾段）。"""
    pre = pre[-16:]
    if "×" in pre:
        work_part = pre.split("×")[-1]
    else:
        # 去掉 @ 前的块时长/距离（含单位字母, 如 5min/10k/40min）
        work_part = re.sub(r"^[\d.]+[a-z]*[\s]*", "", pre) or pre
    tm = _TIME_UNIT.search(work_part)
    if tm:
        v = float(tm.group(1))
        unit = tm.group(2)
        secs = v * 60 if unit in ("mins", "min", "mim") else v
        if secs > 28800:  # >8h 必为文件名粘连误判, 丢弃
            return None
        return {"kind": "time", "value_s": int(round(secs))}
    dmm = _DIST_UNIT.search(work_part)
    if dmm:
        v = float(dmm.group(1))
        meters = v * 1000 if dmm.group(2) == "k" else v
        if meters > 160000:  # >160km 必为粘连误判, 丢弃
            return None
        return {"kind": "distance", "value_m": int(round(meters))}
    bm = re.match(r"^(\d{2,4})$", work_part.strip("() "))
    if bm:
        return {"kind": "distance", "value_m": int(bm.group(1))}
    return None


def _extract_name_annotations(name: str) -> List[Dict[str, Any]]:
    """提取文件名中所有 @ 标注（去掉 .fit 后缀与结尾活动 ID）。"""
    base = name[:-4] if name.lower().endswith(".fit") else name
    base = re.sub(r"-\d{7,10}$", "", base)
    out = []
    for m in re.finditer(r"@", base):
        pre = base[max(0, m.start() - 16):m.start()]
        post = base[m.end():m.end() + 18]
        tkm = re.match(r"^([^@]{1,14})", post)
        token = tkm.group(1) if tkm else ""
        target = _parse_target_token(token)
        ann: Dict[str, Any] = {"target": target,
                               "work": _extract_work(pre),
                               "reps": None, "recovery": None}
        # 组数: N× 且 × 前数字串 ≤3 位（4 位是粘连目标值如 @127-1596×）
        rm = re.search(r"(?<!\d)(\d{1,3})\s*[×x]", pre)
        if rm:
            ann["reps"] = int(rm.group(1))
        # 目标消耗长度（数值 token 精确到数字串末尾; 字母/文字取整段）
        consumed = target.get("consumed", len(token))
        # 恢复段: 目标后 _Ns / _Nmin（如 20s@436-425_60s 的 60s 恢复）
        after = post[consumed:]
        rcm = re.match(r"^[_\-](\d{1,3})\s*(s|sec|mins|min|mim)?\)?", after)
        if rcm:
            v = float(rcm.group(1))
            unit = rcm.group(2)
            ann["recovery"] = int(round(v * 60 if unit in ("mins", "min", "mim") else v))
        # 4 位 hi 末位 + 后续 × => 重复次数（如 @127-1596×10s）
        if target.get("kind") == "range" and target.get("hi_extra") is not None:
            if re.match(r"^\d+\s*×", after) or re.match(r"^×", after):
                ann["reps"] = ann["reps"] or target["hi_extra"]
                target["hi_extra"] = None
        # ×N 后的 工作_恢复 时长对（如 6×10s_110s => 10s 快段 / 110s 恢复）
        wrm = re.match(r"^[×x]?\s*(\d+)\s*(s|sec|min|mim)?\s*[_\-]\s*(\d+)\s*(s|sec|min|mim)?", after)
        if wrm and ann["work"] is None:
            wv = float(wrm.group(1))
            wu = wrm.group(2)
            ann["work"] = {"kind": "time",
                           "value_s": int(round(wv * 60 if wu in ("min", "mim") else wv))}
            rv = float(wrm.group(3))
            ru = wrm.group(4)
            ann["recovery"] = ann["recovery"] or int(
                round(rv * 60 if ru in ("min", "mim") else rv))
        out.append(ann)
    return out


def _fmt_time_s(secs: Optional[float]) -> Optional[str]:
    if secs is None:
        return None
    if secs >= 60 and int(secs) % 60 == 0:
        return f"{secs // 60}min"
    return f"{secs:g}s"


def _target_display(t: Dict[str, Any]) -> Optional[str]:
    k = t.get("kind")
    if k == "range":
        lo_c, hi_c = t.get("lo_cls"), t.get("hi_cls")
        if lo_c.get("kind") == "pace":
            lo_s, hi_s = lo_c["s_per_km"], hi_c["s_per_km"]
            return _pace_display([hi_s, lo_s])  # 快端在前
        if lo_c.get("kind") == "heart_rate":
            return f"{t['lo']}-{t['hi']}bpm"
    if k == "single":
        c = t.get("lo_cls")
        if c.get("kind") == "pace":
            return _pace_display([c["s_per_km"]])
        if c.get("kind") == "heart_rate":
            return f"{t['lo']}bpm"
    if k == "zone":
        return t.get("label")
    if k == "text":
        return t.get("value")
    if k == "hr_zone":
        return f"心率区{t['value'][0]}-{t['value'][1]}"
    return None


def _name_alignment(name: str, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """文件名 @ 标注 vs FIT workout_step 对齐。

    规则（与 skill 哲学一致）: FIT workout_step 是第一证据; 文件名标注是
    用户简写。两者核对后分别陈述, 矛盾时不自动裁决。
    """
    anns = _extract_name_annotations(name)
    if not anns:
        return {"annotations": [],
                "note": "文件名无 @ 标注；训练结构以 workout_steps 为准。"}
    steps_present = bool(steps)
    used: set = set()
    results = []
    for ann in anns:
        t = ann["target"]
        r: Dict[str, Any] = {
            "target": t,
            "target_display": _target_display(t),
            "work": ann["work"],
            "reps": ann["reps"],
            "recovery_s": ann["recovery"],
            "matched_step": None, "step_target": None,
            "aligned": None, "notes": [],
        }
        if t.get("kind") in ("pace", "range", "single") and steps_present:
            want = []
            if t.get("kind") == "range":
                if t["lo_cls"]["kind"] == "pace" and t["hi_cls"]["kind"] == "pace":
                    want = ("pace", [t["lo_cls"]["s_per_km"], t["hi_cls"]["s_per_km"]])
                elif t["lo_cls"]["kind"] == "heart_rate":
                    want = ("hr", [t["lo"], t["hi"]])
            elif t.get("kind") == "single":
                c = t["lo_cls"]
                if c["kind"] == "pace":
                    want = ("pace", [c["s_per_km"], c["s_per_km"]])
                elif c["kind"] == "heart_rate":
                    want = ("hr", [t["lo"], t["lo"]])
            if want:
                kind, vals = want
                best, best_err = None, None
                for s in steps:
                    if s["index"] in used:
                        continue
                    if kind == "pace" and s.get("target_pace_s_per_km"):
                        sl, sh = s["target_pace_s_per_km"]
                        err = min(min(abs(a - sl), abs(a - sh)) for a in vals)
                    elif kind == "hr" and s.get("target_heart_rate"):
                        sl, sh = s["target_heart_rate"]
                        if len(vals) == 1:
                            err = 0.0 if sl <= vals[0] <= sh else min(
                                abs(vals[0] - sl), abs(vals[0] - sh))
                        else:
                            if max(vals[0], sl) <= min(vals[1], sh):
                                err = 0.0  # 区间重叠
                            else:
                                err = min(abs(a - b) for a in vals for b in (sl, sh))
                    else:
                        continue
                    if best_err is None or err < best_err:
                        best, best_err = s, err
                if best is not None and best_err is not None:
                    tol = 10.0  # s/km 或 bpm
                    r["matched_step"] = best["index"]
                    used.add(best["index"])
                    r["step_target"] = best.get("target_pace_display") \
                        or (f"{best['target_heart_rate'][0]}-{best['target_heart_rate'][1]}bpm"
                            if best.get("target_heart_rate") else best.get("target_type"))
                    r["step_duration_s"] = best.get("duration_time_s")
                    r["aligned"] = bool(best_err <= tol)
                    if not r["aligned"]:
                        r["notes"].append(
                            f"文件名目标与设备记录接近（差 {best_err:g}）但不完全一致；以 workout_step 为准，可核对课表")
                    if (r["aligned"] and ann["work"] and ann["work"]["kind"] == "time"
                            and best.get("duration_time_s")):
                        if abs(ann["work"]["value_s"] - best["duration_time_s"]) > 2:
                            r["notes"].append(
                                f"文件名标工作段 {_fmt_time_s(ann['work']['value_s'])}，"
                                f"设备记录 {best['duration_time_s']:g}s；以设备记录为准")
        else:
            # 区间字母/心率分区/文字标注: 无法数值对齐, 给设备实际目标供参考
            if steps_present:
                hr_steps = [s for s in steps if s.get("target_heart_rate")]
                if t.get("kind") == "hr_zone":
                    ref = (hr_steps or [None])[0]
                else:
                    ref = (hr_steps or steps)[0] \
                        if (hr_steps or steps) else None
                if ref is not None:
                    r["step_target"] = (f"{ref['target_heart_rate'][0]:g}-{ref['target_heart_rate'][1]:g}bpm"
                                        if ref.get("target_heart_rate")
                                        else ref.get("target_pace_display"))
            if t.get("kind") == "hr_zone":
                r["notes"].append(
                    "心率分区标注；分区↔bpm 映射随静息/最大心率变化，"
                    "FIT 未存分区边界，无法从文件核对，按设备段实测心率区间陈述")
            else:
                r["notes"].append("非数值标注（区间标签/文字），不与 workout_step 数值对齐")
        results.append(r)
    note = ("FIT workout_step 为第一证据；文件名标注为课表简写。"
            "aligned=false 或缺 matched_step 时分别陈述，不自动裁决。"
            if steps_present else
            "FIT 中无 workout_step 记录，仅报告文件名标注的解析结果。")
    return {"annotations": results, "note": note}



def _record_rows(msgs: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for r in msgs.get("record_mesgs", []):
        rows.append({
            "t": _num(r.get("timestamp")),
            "hr": _num(r.get("heart_rate")),
            "cad": _num(r.get("cadence")),
            "pow": _num(r.get("power")),
            "spd": _num(r.get("enhanced_speed")) or _num(r.get("speed")),
            "dist": _num(r.get("distance")),
            "alt": _num(r.get("enhanced_altitude")),
            "temp": _num(r.get("temperature")),
            "type": r.get("activity_type"),
        })
    return rows


def _env_quality(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """环境字段质量：温度/湿度是否可信（决定是否走天气补全分支）。"""
    temps = [r["temp"] for r in records if r["temp"] is not None]
    valid_temps = [t for t in temps if 0 <= t <= 45]
    if not temps:
        return {"temperature": {"status": MISSING}, "humidity": {"status": MISSING}}
    frac = len(valid_temps) / len(temps)
    status = OK if frac >= 0.95 else (PARTIAL if frac > 0 else MISSING)
    return {
        "temperature": {
            "status": status,
            "mean_c": _round(_mean(valid_temps), 1) if valid_temps else None,
            "min_c": _round(_min(valid_temps), 1) if valid_temps else None,
            "max_c": _round(_max(valid_temps), 1) if valid_temps else None,
        },
        "humidity": {"status": MISSING},  # Garmin FIT 一般无湿度字段
    }


def _build_summary(name: str, path: str) -> Dict[str, Any]:
    msgs = _decode(path)
    session = _session_block(msgs)
    records = _record_rows(msgs)
    laps = msgs.get("lap_mesgs", [])

    hr_vals = [r["hr"] for r in records]
    cad_vals = [r["cad"] for r in records if r["cad"] and r["cad"] < 300]
    # 心率漂移：同段前后 1/4 的平均心率差（粗略，供参考）
    drift = None
    valid_hr = [v for v in hr_vals if v is not None]
    if len(valid_hr) >= 8:
        q = max(1, len(valid_hr) // 4)
        drift = _round(_mean(valid_hr[-q:]) - _mean(valid_hr[:q]), 1)

    steps = _workout_block(msgs)
    out: Dict[str, Any] = {
        "source": "local FIT",
        "file": name,
        "session": session,
        "workout_steps": steps,
        "name_alignment": _name_alignment(name, steps),
        "quality": {
            "records": len(records),
            "heart_rate": _coverage(records, "hr"),
            "cadence": _coverage(records, "cad"),
            "power": _coverage(records, "pow"),
            "speed": _coverage(records, "spd"),
            "altitude": _coverage(records, "alt"),
            "env": _env_quality(records),
        },
        "derived": {
            "avg_cadence": _round(_mean(cad_vals), 1),
            "hr_drift_last_q_minus_first_q": drift,
            "pace_s_per_km_avg": _round(
                _s2pace_ms(session.get("avg_speed_mps")), 1),
        },
        "lapse_count": len(laps),
        "note": "坐标/路线/起终点按隐私边界不输出；分圈明细用 laps 命令。",
    }
    return out


def _laps(msgs: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for i, l in enumerate(msgs.get("lap_mesgs", [])):
        t = _num(l.get("total_timer_time"))
        d = _num(l.get("total_distance"))
        pace = None
        if d and t:
            pace = _round(t / (d / 1000.0), 1)
        out.append({
            "index": i + 1,
            "timer_time_s": t,
            "distance_m": d,
            "pace_s_per_km": pace,
            "avg_heart_rate": _round(_num(l.get("avg_heart_rate")), 1),
            "max_heart_rate": _num(l.get("max_heart_rate")),
            "avg_power": _round(_num(l.get("avg_power")), 1),
            "ascent_m": _round(_num(l.get("total_ascent")), 1),
            "descent_m": _round(_num(l.get("total_descent")), 1),
            "avg_cadence": _round(_num(l.get("avg_cadence")), 1),
            "total_calories": _num(l.get("total_calories")),
        })
    return out


def _window(
    msgs: Dict[str, Any],
    last_minutes: Optional[float],
    minutes_from_start: Optional[float],
    minutes_to: Optional[float],
    sample: int,
) -> Dict[str, Any]:
    records = _record_rows(msgs)
    t0 = None
    for sess in msgs.get("session_mesgs", []):
        if sess.get("start_time") is not None:
            t0 = int(sess["start_time"])
            break
    if t0 is None and records:
        t0 = int(records[0]["t"])
    if t0 is None:
        raise RuntimeError("no session start time and no records")

    t_end = None
    if last_minutes is not None:
        t_end = None
        t_start = (max(records, key=lambda r: r["t"] or 0))["t"] - last_minutes * 60
    else:
        t_start = t0 + (minutes_from_start or 0) * 60
        t_end = t0 + minutes_to * 60 if minutes_to else None

    rows = [
        r for r in records
        if (r["t"] or 0) >= t_start and (t_end is None or (r["t"] or 0) <= t_end)
    ]
    if sample and len(rows) > sample:
        step = len(rows) / sample
        rows = [rows[int(i * step)] for i in range(sample)]
    return {
        "window": {
            "from_s": (t_start - t0) if last_minutes is None else None,
            "last_minutes": last_minutes,
            "to_s": (t_end - t0) if t_end and last_minutes is None else None,
        },
        "points": len(rows),
        "records": [
            {
                "t_offset_s": _round((r["t"] or t0) - t0, 0),
                "hr": r["hr"],
                "cad": r["cad"],
                "pow": r["pow"],
                "spd_mps": _round(r["spd"], 3),
                "pace_s_per_km": _round(_s2pace_ms(r["spd"]), 1),
                "alt_m": _round(r["alt"], 1),
                "temp_c": r["temp"],
            }
            for r in rows
        ],
    }


# ---------------- CLI ----------------

def cmd_list(args: argparse.Namespace) -> int:
    fit_dir = _resolve_dir(args.dir)
    if not os.path.isdir(fit_dir):
        print(f"错误：FIT 目录不存在: {fit_dir}", file=sys.stderr)
        return 2
    files = _list_fit_files(fit_dir)
    if not files:
        print(f"错误：FIT 目录中没有 .fit 文件: {fit_dir}", file=sys.stderr)
        return 2

    cutoff = None
    if args.days:
        cutoff = datetime.now() - timedelta(days=args.days)

    items = []
    for name in files:
        meta = _file_meta(name, os.path.join(fit_dir, name))
        if args.days:
            d = meta.get("date")
            if not d:
                # 无法从文件名解析时间时打开文件取 start_time（代价可控：只解 file_id/session）
                try:
                    msgs = _decode(os.path.join(fit_dir, name))
                    st = _local_start_time(msgs)
                except Exception:
                    continue
                if not st:
                    continue
                if datetime.fromisoformat(st).replace(tzinfo=timezone.utc) < cutoff.replace(tzinfo=timezone.utc):
                    continue
            else:
                try:
                    fdt = datetime.strptime(d + meta.get("time", "000000"), "%Y%m%d%H%M%S")
                    if fdt < cutoff:
                        continue
                except ValueError:
                    pass
        if args.sport:
            want = _parse_args_sport(args.sport)
            iname = (meta.get("sport_in_name") or "").lower()
            if want not in iname:
                continue
        items.append(meta)
    items.reverse()  # 最新在前
    items = items[: args.limit]
    print(json.dumps(
        {"fit_dir": fit_dir, "count": len(items), "activities": items},
        ensure_ascii=False, indent=1))
    return 0


def _find_file(args: argparse.Namespace) -> Optional[str]:
    fit_dir = _resolve_dir(args.dir)
    name = args.file
    path = name if os.path.sep in name or name.lower().endswith(".fit") \
        else os.path.join(fit_dir, name if name.lower().endswith(".fit") else name + ".fit")
    if os.path.isfile(path):
        return path
    # 模糊匹配
    base = os.path.basename(name)
    for f in _list_fit_files(fit_dir):
        if base in f or (len(base) >= 8 and base in f):
            return os.path.join(fit_dir, f)
    return None


def cmd_summary(args: argparse.Namespace) -> int:
    path = _find_file(args)
    if not path:
        print("错误：找不到该 FIT 文件", file=sys.stderr)
        return 4
    try:
        out = _build_summary(os.path.basename(path), path)
    except Exception as e:
        print(f"解析失败: {e}", file=sys.stderr)
        return 3
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_laps(args: argparse.Namespace) -> int:
    path = _find_file(args)
    if not path:
        print("错误：找不到该 FIT 文件", file=sys.stderr)
        return 4
    try:
        msgs = _decode(path)
    except Exception as e:
        print(f"解析失败: {e}", file=sys.stderr)
        return 3
    laps = _laps(msgs)
    if args.max_laps and len(laps) > args.max_laps:
        # 保留首尾各一半，中间用标记代替
        half = args.max_laps // 2
        kept = laps[:half] + laps[-half:]
        out = {"file": os.path.basename(path), "lap_count": len(laps),
               "note": f"只显示首尾各 {half} 圈，中间省略",
               "laps": kept}
    else:
        out = {"file": os.path.basename(path), "lap_count": len(laps), "laps": laps}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_window(args: argparse.Namespace) -> int:
    path = _find_file(args)
    if not path:
        print("错误：找不到该 FIT 文件", file=sys.stderr)
        return 4
    try:
        msgs = _decode(path)
        out = _window(
            msgs,
            last_minutes=args.last_minutes,
            minutes_from_start=args.from_minutes,
            minutes_to=args.to_minutes,
            sample=args.sample,
        )
        out["file"] = os.path.basename(path)
    except Exception as e:
        print(f"解析失败: {e}", file=sys.stderr)
        return 3
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", help="FIT 文件夹路径（覆盖环境变量与默认值）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="活动列表（只给摘要）")
    pl.add_argument("--days", type=float, default=14, help="最近 N 天，0=全部")
    pl.add_argument("--sport", help="按运动类型过滤，如 running")
    pl.add_argument("--limit", type=int, default=10)
    pl.set_defaults(fn=cmd_list)

    ps = sub.add_parser("summary", help="活动详情 + 数据质量")
    ps.add_argument("--file", required=True, help="文件名（或完整路径/唯一子串）")
    ps.set_defaults(fn=cmd_summary)

    pp = sub.add_parser("laps", help="分圈/分段")
    pp.add_argument("--file", required=True)
    pp.add_argument("--max-laps", type=int, default=40,
                    help="超过时只保留首尾各一半")
    pp.set_defaults(fn=cmd_laps)

    pw = sub.add_parser("window", help="自定义时间窗")
    pw.add_argument("--file", required=True)
    pw.add_argument("--last-minutes", type=float, help="最后 N 分钟")
    pw.add_argument("--from-minutes", type=float, help="从开始第 N 分钟起")
    pw.add_argument("--to-minutes", type=float, help="到第 N 分钟止（与 from 搭配）")
    pw.add_argument("--sample", type=int, default=60, help="最多输出点数")
    pw.set_defaults(fn=cmd_window)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
