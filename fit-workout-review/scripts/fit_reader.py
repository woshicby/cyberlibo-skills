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
from typing import Any, Dict, List, Optional

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
            step["target_pace_s_per_km"] = [
                _round(_s2pace_ms(speed_hi), 1), _round(_s2pace_ms(speed_lo), 1)]
        if tgt == "heart_rate" and (hr_lo or hr_hi):
            step["target_heart_rate"] = [hr_lo, hr_hi]
        steps.append(step)
    return steps


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

    out: Dict[str, Any] = {
        "source": "local FIT",
        "file": name,
        "session": session,
        "workout_steps": _workout_block(msgs),
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
