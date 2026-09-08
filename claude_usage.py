#!/usr/bin/env python3
"""Read-only local Claude session accounting and rate-limited quota snapshots."""
import argparse
import collections
import hashlib
try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt
import json
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BJ = timezone(timedelta(hours=8))
RATES = {
    "claude-fable-5-1": (10, 50, .25, 20, 12.5),
    "claude-opus-5": (5, 25, .5, 10, 6.25),
}
CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
CACHE_BASE = Path(os.environ.get("LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME",
                                 str(Path.home() / ".cache"))) / "claude-usage-lite"
CREDENTIALS = CONFIG_DIR / ".credentials.json"
CACHE = CACHE_BASE
FILE_CACHE = {}

def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def stamp(value):
    if value is None:
        return "未知"
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    return value.astimezone(BJ).strftime("%m-%d %H:%M:%S")

def acquire_lock(handle):
    """Nonblocking process lock, released automatically when the handle closes."""
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except (BlockingIOError, OSError):
        return False

def read_state():
    try:
        return json.loads((CACHE / "quota.json").read_text())
    except (OSError, ValueError):
        return {}

def save_state(state):
    tmp = CACHE / ("quota.%d.tmp" % os.getpid())
    with tmp.open("w") as f:
        os.chmod(tmp, 0o600)
        json.dump(state, f)
    tmp.replace(CACHE / "quota.json")

def quota(local_only=False):
    if local_only:
        state = read_state()
        return state, "仅本地；额度缓存"
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (CACHE / "quota.lock").open("a+b") as lock:
        if not acquire_lock(lock):
            return read_state(), "其他进程正在查询；额度缓存"
        state = read_state()
        now = time.time()
        if now < state.get("next_query", 0):
            return state, "额度缓存；下次查询 " + stamp(state["next_query"])
        # Persist before querying so interrupted/restarted processes do not hammer the endpoint.
        state["next_query"] = now + 600
        save_state(state)
        paths = [CREDENTIALS]
        error = "无可用凭据"
        seen = set()
        for path in paths:
            try:
                token = json.loads(path.read_text())["claudeAiOauth"]["accessToken"]
                if not isinstance(token, str) or not token:
                    continue
                if token in seen:
                    continue
                seen.add(token)
                req = Request("https://api.anthropic.com/api/oauth/usage", headers={
                    "Authorization": "Bearer " + token,
                    "anthropic-beta": "oauth-2025-04-20",
                })
                with urlopen(req, timeout=15) as response:
                    data = json.load(response)
                state.update(data=data, fetched_at=time.time(), error=None)
                save_state(state)
                return state, "额度已刷新"
            except HTTPError as e:
                error = "HTTP %s" % e.code
                if e.code == 401:
                    continue
                if e.code == 429:
                    retry = e.headers.get("Retry-After", "")
                    try:
                        seconds = float(retry)
                    except ValueError:
                        try:
                            seconds = parsedate_to_datetime(retry).timestamp() - time.time()
                        except (ValueError, TypeError, OverflowError):
                            seconds = 3600
                    if not math.isfinite(seconds):
                        seconds = 3600
                    state["next_query"] = time.time() + max(600, seconds)
                break
            except (OSError, ValueError, KeyError, TypeError):
                error = "凭据或网络不可用"
        state["error"] = error
        save_state(state)
        return state, error + "；下次查询 " + stamp(state["next_query"])

def limits_and_window(state, now):
    result = {}
    start = None
    fetched = state.get("fetched_at", 0)
    for item in (state.get("data") or {}).get("limits", []):
        if item.get("percent") is None:
            continue
        try:
            end = dt(item["resets_at"]) if item.get("resets_at") else None
        except (ValueError, TypeError):
            continue
        name = {"session": "5小时", "weekly_all": "总周"}.get(item.get("kind"))
        if not name:
            name = ((item.get("scope") or {}).get("model") or {}).get("display_name")
        if not name:
            continue
        expired = end is not None and now >= end
        result[name] = dict(percent=item["percent"], reset=end, expired=expired)
        if item.get("kind") == "session" and end and not expired:
            start = end - timedelta(hours=5)
    return result, start, fetched

def load_file(path):
    st = path.stat()
    signature = (st.st_mtime_ns, st.st_size)
    cached = FILE_CACHE.get(str(path))
    if cached and cached[0] == signature:
        return cached[1]
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.endswith("\n"):
                continue  # an in-progress append is retried on the next scan
            try:
                row = json.loads(line)
                msg = row.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    continue
                model = msg.get("model")
                if not model or model.startswith("<"):
                    continue
                timestamp = dt(row["timestamp"])
                if timestamp.tzinfo is None:
                    continue
                ident = row.get("requestId") or msg.get("id") or row.get("uuid")
                if not ident:
                    continue
                key = (ident, msg.get("id"), model)
                rows.append(dict(key=key, time=timestamp, model=model, usage=msg["usage"],
                                 cwd=row.get("cwd") or ""))
            except (ValueError, KeyError, TypeError):
                continue
    FILE_CACHE[str(path)] = (signature, rows)
    return rows

def collect(hours, requested, now, roots=None):
    cutoff = now - timedelta(hours=hours)
    roots = roots if roots is not None else {CONFIG_DIR / "projects"}
    candidates = {}
    warnings = []
    for root in sorted(set(roots)):
        try:
            for path in root.glob("**/*.jsonl"):
                rel = path.relative_to(root)
                parts = rel.parts
                if len(parts) == 2:
                    sid, sub = path.stem, False
                elif len(parts) == 4 and parts[2] == "subagents":
                    sid, sub = parts[1], True
                else:
                    continue
                try:
                    uuid.UUID(sid)
                except ValueError:
                    continue
                if requested and sid != requested:
                    continue
                candidates.setdefault(sid, []).append((path, sub, root.parent.parent.name))
        except OSError:
            warnings.append("部分目录无法扫描")
    records, metadata = {}, {}
    for sid, files in sorted(candidates.items()):
        try:
            if not requested and not any(p.stat().st_mtime >= cutoff.timestamp() for p, _, _ in files):
                continue
        except OSError:
            continue
        metadata[sid] = dict(users=set(), cwd="", subagents=set())
        for path, sub, user in files:
            try:
                for item in load_file(path):
                    item = dict(item, sid=sid, sub=sub)
                    old = records.get(item["key"])
                    # Assign copies to earliest occurrence; choose latest usage record within its owner.
                    if old is None or (old["sid"] == sid and item["time"] >= old["time"]) or (
                        old["sid"] != sid and item["time"] < old["time"]
                    ):
                        records[item["key"]] = item
                    if item["cwd"] and not sub:
                        metadata[sid]["cwd"] = item["cwd"]
                metadata[sid]["users"].add(user)
                if sub:
                    metadata[sid]["subagents"].add(str(path))
            except OSError:
                warnings.append("部分日志不可读")
    groups = collections.defaultdict(list)
    for item in records.values():
        groups[item["sid"]].append(item)
    selected = {}
    for sid, rows in groups.items():
        if requested or any(cutoff <= r["time"] <= now for r in rows):
            selected[sid] = rows
    return selected, metadata, cutoff, sorted(set(warnings))

def totals(rows):
    result = dict(requests=len(rows), input=0, output=0, thinking=0, read=0,
                  write=0, usd=0.0, unpriced=0, models=set())
    for row in rows:
        u = row["usage"]
        for dest, source in [("input","input_tokens"), ("output","output_tokens"),
                             ("read","cache_read_input_tokens"), ("write","cache_creation_input_tokens")]:
            result[dest] += u.get(source, 0) or 0
        result["thinking"] += (u.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0
        result["models"].add(row["model"])
        rates = RATES.get(row["model"])
        cache = u.get("cache_creation") or {}
        one = cache.get("ephemeral_1h_input_tokens", 0) or 0
        five = cache.get("ephemeral_5m_input_tokens", 0) or 0
        if not rates or u.get("speed", "standard") not in ("standard", None) or (
            one + five != (u.get("cache_creation_input_tokens", 0) or 0)
        ):
            result["unpriced"] += 1
            continue
        ip, op, read, w1, w5 = rates
        result["usd"] += ((u.get("input_tokens",0) or 0)*ip +
                          (u.get("output_tokens",0) or 0)*op +
                          (u.get("cache_read_input_tokens",0) or 0)*read + one*w1 + five*w5)/1e6
        result["usd"] += ((u.get("server_tool_use") or {}).get("web_search_requests",0) or 0)*.01
    return result

def money(t):
    return "$%.4f%s" % (t["usd"], "*" if t["unpriced"] else "")

def snapshot(args):
    now = datetime.now(timezone.utc)
    state, note = quota(not args.api)
    limits, window, fetched = limits_and_window(state, now)
    groups, metadata, cutoff, warnings = collect(args.hours, args.session_id, now, args.roots)
    all_rows = [r for rows in groups.values() for r in rows]
    recent = [r for r in all_rows if cutoff <= r["time"] <= now]
    within = [r for r in all_rows if window <= r["time"] <= now] if window else None
    return dict(now=now, note=note, state=state, limits=limits, window=window,
                fetched=fetched, groups=groups, metadata=metadata, cutoff=cutoff,
                all=totals(all_rows), recent=totals(recent),
                within=totals(within) if within is not None else None, warnings=warnings)

def render(s, args, compact=False, previous=None):
    parts = [stamp(s["now"]) + " 北京时间"]
    for name, info in s["limits"].items():
        if info["expired"]:
            parts.append(name + " 缓存已过期")
            continue
        val = info["percent"]
        delta = ""
        if previous and name in previous:
            diff = val - previous[name]
            delta = ("(%+g)" % diff) if diff >= 0 else "(重置/回落)"
        parts.append("%s %g%%%s" % (name, val, delta))
    if not s["limits"]:
        parts.append("账号额度未知")
    if compact:
        parts += ["会话 %d" % len(s["groups"]), "最近%gh %s" % (args.hours,money(s["recent"])),
                  "全历史合计 "+money(s["all"]),
                  "本窗口 "+(money(s["within"]) if s["within"] is not None else "未知"),
                  "请求 %d" % s["recent"]["requests"], s["note"]]
        if s["fetched"]:
            parts.append("额度采集 "+stamp(s["fetched"]))
        print(" | ".join(parts), flush=True)
    else:
        print("\n".join(parts))
        print("额度状态："+s["note"])
        if s["fetched"]:
            print("额度采集："+stamp(s["fetched"])+"；缓存越过重置点不继续当作当前值。")
        print("\n最近 %g 小时有调用的会话（指定 ID 时显示该会话全部历史）" % args.hours)
        print("最后调用(北京时间) | 用户 | 任务/目录 | 会话ID | 模型 | 全历史请求 | 全历史$ | 最近%gh$ | 本窗口$ | 子代理请求" % args.hours)
        ordered = sorted(s["groups"], key=lambda sid:max(r["time"] for r in s["groups"][sid]), reverse=True)
        for sid in ordered:
            rows = s["groups"][sid]
            meta = s["metadata"][sid]
            lifetime = totals(rows)
            recent = totals([r for r in rows if s["cutoff"] <= r["time"] <= s["now"]])
            current = totals([r for r in rows if s["window"] <= r["time"] <= s["now"]]) if s["window"] else None
            cwd = meta["cwd"]
            label = Path(cwd).name if cwd else "未知"
            print(" | ".join([stamp(max(r["time"] for r in rows)), ",".join(sorted(meta["users"])),
                label, sid, ",".join(sorted(lifetime["models"])), str(lifetime["requests"]),
                money(lifetime), money(recent), money(current) if current is not None else "未知",
                str(sum(r["sub"] for r in rows))]))
        print("\n所选会话全历史去重合计："+money(s["all"]))
        print("最近 %g 小时内合计：%s（%d 次请求）" % (args.hours,money(s["recent"]),s["recent"]["requests"]))
        print("当前窗口内合计："+(money(s["within"]) if s["within"] is not None else "未知；需要有效的窗口重置时间"))
        print("最近时段 token：输入 {input:,} ｜ 输出 {output:,}（其中思考 {thinking:,}）｜ 缓存读 {read:,} ｜ 缓存写 {write:,}".format(**s["recent"]))
    for warning in s["warnings"]:
        print("注意："+warning)
    if not compact:
        print("\n金额是已计价 API 标价等价值，不是订阅扣款；* 表示含未计价请求，金额不完整。")
        print("只统计所配置的本地目录（含子代理），按请求去重；不包含未提供的设备或网页用量。")
        print("当前窗口按请求日志时间估算；全历史合计不能直接当作当前窗口或最近时段成本。")

def main():
    global CACHE, CREDENTIALS, RATES
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="最近所有 Claude 会话与成本汇总；不调用模型。")
    p.add_argument("session_id", nargs="?", help="可选：只统计指定会话 UUID")
    p.add_argument("--hours", type=float, default=24, help="最近多少小时，默认 24")
    p.add_argument("--status", action="store_true", help="持续追加汇总行")
    p.add_argument("--interval", type=int, default=600, help="本地刷新间隔秒数，默认 600，最小 60")
    p.add_argument("--count", type=int, help="监测次数上限")
    network = p.add_mutually_exclusive_group()
    network.add_argument("--api", action="store_true", help="显式启用实验性账号额度接口；至少间隔 10 分钟")
    network.add_argument("--local", action="store_true", help="只读取本地日志和缓存（默认行为）")
    p.add_argument("--data-dir", action="append", help="Claude 配置目录或 projects 目录；可重复指定")
    p.add_argument("--credentials", type=Path, help="--api 使用的凭据文件；默认当前配置目录")
    p.add_argument("--pricing", type=Path, help="JSON 模型单价文件；每百万 token 的美元价格")
    p.add_argument("--version", action="version", version="claude-usage-lite 0.1.0")
    args = p.parse_args()
    args.roots = set()
    for value in args.data_dir or [str(CONFIG_DIR)]:
        folder = Path(value).expanduser().resolve()
        args.roots.add(folder if folder.name == "projects" else folder / "projects")
    CREDENTIALS = (args.credentials or (CONFIG_DIR / ".credentials.json")).expanduser().resolve()
    CACHE = CACHE_BASE / hashlib.sha256(str(CREDENTIALS).encode()).hexdigest()[:16]
    if args.pricing:
        try:
            custom = json.loads(args.pricing.read_text(encoding="utf-8"))
            if not isinstance(custom, dict):
                raise ValueError("价格文件必须是对象")
            for model, rates in custom.items():
                values = tuple(rates[key] for key in ("input", "output", "cache_read", "cache_write_1h", "cache_write_5m"))
                if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0 for x in values):
                    raise ValueError("单价必须是非负有限数字")
                RATES[model] = values
        except (OSError, ValueError, KeyError, TypeError):
            p.error("无法读取模型单价，请检查 JSON 格式和五项单价")
    if not math.isfinite(args.hours) or args.hours <= 0 or args.interval < 60 or (args.count is not None and args.count < 1):
        p.error("hours 必须为正数；interval 至少 60；count 至少 1")
    if args.session_id:
        try:
            args.session_id = str(uuid.UUID(args.session_id))
        except ValueError:
            p.error("会话 ID 必须是 UUID")
    if not args.status:
        render(snapshot(args), args)
        return
    print("最近 %g 小时所有会话%s ｜ 每 %d 秒追加一行 ｜ Ctrl+C 停止" % (
        args.hours, "（指定 "+args.session_id+"）" if args.session_id else "",args.interval),flush=True)
    print("接口等待期间本地统计照常刷新；查询间隔至少10分钟，重启也保留等待期限。",flush=True)
    previous, count = {}, 0
    try:
        while True:
            begin = time.monotonic()
            s = snapshot(args)
            render(s,args,True,previous)
            previous = {k:v["percent"] for k,v in s["limits"].items() if not v["expired"]}
            count += 1
            if args.count and count >= args.count:
                break
            time.sleep(max(0,args.interval-(time.monotonic()-begin)))
    except KeyboardInterrupt:
        print("\n已停止监测。",flush=True)

if __name__ == "__main__":
    main()

