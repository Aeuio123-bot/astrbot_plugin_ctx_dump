"""astrbot_plugin_ctx_dump

上下文落盘 + 可视化：把每一次发往 LLM 的 ProviderRequest 原样存下来，
并在两个地方看它：AstrBot WebUI 的插件视图，或插件自带的本地小页面。

为什么自带本地页面：AstrBot 的插件视图依赖 dashboard（桌面端即 Tauri 外壳加载
localhost:6185）。若那个入口不可用，插件可以用 stdlib 起一个只绑 127.0.0.1 的
HTTP 服务，直接返回同样的页面和 JSON，卸载时随进程一起结束。

数据源：<AstrBot data>/plugin_data/ctx_dump/YYYY-MM-DD.jsonl
        每条记录 = 一次 LLM 请求（agent 工具循环里每一轮各一条）。
页面：  插件详情 → 上下文时间线；或 http://127.0.0.1:<local_port>
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_ctx_dump"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+"
    r"|(\"?(?:api_?key|token|secret)\"?\s*[:=]\s*\")([^\"]{6,})\"",
    re.IGNORECASE,
)

CATS = ("system", "tools", "user", "assistant", "tool_calls", "tool_result", "reasoning")


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def estimate_tokens(text: str) -> int:
    """粗估 token：CJK 按 1 token/字，其余按 4 字符/token。只做相对比较。"""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF:
            cjk += 1
    return int(cjk + (len(text) - cjk) / 4) + 1


def _safe(obj: Any, depth: int = 0) -> Any:
    if depth > 20:
        return "<max-depth>"
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_safe(v, depth + 1) for v in obj]
    for attr in ("to_dict", "model_dump", "dict", "__dict__"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _safe(fn(), depth + 1)
            except Exception:
                continue
    return str(obj)


def _dump_tools(func_tool: Any) -> dict:
    out: dict[str, Any] = {"names": [], "schemas": []}
    if not func_tool:
        return out
    try:
        out["names"] = list(func_tool.names())
    except Exception:
        pass
    for tool in getattr(func_tool, "func_list", None) or []:
        out["schemas"].append(
            {
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", ""),
                "parameters": _safe(getattr(tool, "parameters", {})),
            }
        )
    return out


def _msg_text(msg: Any) -> str:
    if not isinstance(msg, dict):
        return str(msg)
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(p.get("text") or json.dumps(p, ensure_ascii=False))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def _breakdown(record: dict) -> dict:
    b = dict.fromkeys(CATS, 0)
    b["system"] = estimate_tokens(record.get("system_prompt") or "")

    tools = record.get("tools") or {}
    if tools.get("schemas"):
        b["tools"] = estimate_tokens(json.dumps(tools["schemas"], ensure_ascii=False))
    elif tools.get("names"):
        b["tools"] = estimate_tokens(json.dumps(tools["names"], ensure_ascii=False))

    for msg in record.get("contexts") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        text = _msg_text(msg)
        n = estimate_tokens(text)
        if msg.get("reasoning_content"):
            b["reasoning"] += estimate_tokens(str(msg["reasoning_content"]))
        if msg.get("tool_calls"):
            b["tool_calls"] += estimate_tokens(
                json.dumps(msg["tool_calls"], ensure_ascii=False)
            )
        if role == "tool":
            b["tool_result"] += n
        elif role == "assistant":
            b["assistant"] += n
        elif role == "system":
            b["system"] += n
        else:
            b["user"] += n

    b["prompt"] = estimate_tokens(record.get("prompt") or "")
    b["total"] = sum(b[k] for k in CATS) + b["prompt"]
    return b


def _summary(record: dict) -> dict:
    b = _breakdown(record)
    return {
        "seq": record.get("seq"),
        "iso": record.get("iso"),
        "kind": record.get("kind") or "request",
        "umo": record.get("umo") or "",
        "sender": record.get("sender") or "",
        "platform": record.get("platform") or "",
        "model": record.get("model") or "",
        "n_contexts": record.get("n_contexts") or 0,
        "chars": record.get("chars") or 0,
        "tokens": b["total"],
        "breakdown": b,
        "tools": (record.get("tools") or {}).get("names") or [],
    }


# ---------------------------------------------------------------- 纯数据层
# 同时被 WebUI 视图 API 与本地页面复用，避免两套逻辑走偏。
def read_day(data_dir: Path, date: str) -> list[dict]:
    path = data_dir / f"{date}.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def build_sessions(records: list[dict]) -> dict:
    counts: dict[str, dict] = {}
    for rec in records:
        if rec.get("kind") == "response":
            continue
        umo = rec.get("umo") or "(未知会话)"
        item = counts.setdefault(
            umo, {"umo": umo, "sender": rec.get("sender") or "", "steps": 0}
        )
        item["steps"] += 1
    return {"sessions": sorted(counts.values(), key=lambda x: -x["steps"])}


def build_list(records: list[dict], umo: str, limit: int) -> dict:
    steps = []
    for rec in records:
        if rec.get("kind") == "response":
            continue
        if umo and rec.get("umo") != umo:
            continue
        steps.append(_summary(rec))
    steps.sort(key=lambda s: s.get("seq") or 0)
    total = len(steps)
    if limit and total > limit:
        steps = steps[-limit:]
    return {"umo": umo, "total": total, "steps": steps}


def build_detail(records: list[dict], seq: int, full: bool) -> dict | None:
    target = None
    for rec in records:
        if rec.get("kind") == "response" or rec.get("seq") != seq:
            continue
        target = rec
    if target is None:
        return None

    prev = None
    for rec in records:
        if rec.get("kind") == "response" or rec.get("seq") == seq:
            continue
        if rec.get("umo") == target.get("umo") and (rec.get("seq") or 0) < seq:
            if prev is None or (rec.get("seq") or 0) > (prev.get("seq") or 0):
                prev = rec

    detail = dict(target)
    if not full:
        detail["contexts"] = [
            {**m, "content": _msg_text(m)[:4000]} if isinstance(m, dict) else m
            for m in (target.get("contexts") or [])
        ]
    detail["breakdown"] = _breakdown(target)
    detail["prev"] = _summary(prev) if prev else None

    # 逐条消息：估算 token + 与上一轮的关系（first / new / changed / same）
    # AstrBot 通常把新消息追加在末尾，所以按下标对齐；上下文被压缩时可能整体错位，
    # 这种情况会被标成 changed，属于可接受的近似。
    prev_ctx = (prev.get("contexts") or []) if prev else []
    msg_items = []
    n_new = n_changed = 0
    for i, m in enumerate(target.get("contexts") or []):
        if not isinstance(m, dict):
            continue
        if not prev:
            state = "first"
        elif i >= len(prev_ctx):
            state = "new"
            n_new += 1
        elif _msg_text(m).strip() != _msg_text(prev_ctx[i]).strip():
            state = "changed"
            n_changed += 1
        else:
            state = "same"
        msg_items.append(
            {
                "i": i,
                "role": m.get("role") or "user",
                "tokens": estimate_tokens(_msg_text(m)),
                "state": state,
            }
        )
    detail["msg_items"] = msg_items
    detail["diff"] = {
        "new": n_new,
        "changed": n_changed,
        "removed": max(0, len(prev_ctx) - len(target.get("contexts") or [])) if prev else 0,
        "prev_count": len(prev_ctx),
    }
    detail["tool_items"] = [
        {
            "name": s.get("name") or "",
            "tokens": estimate_tokens(json.dumps(s, ensure_ascii=False)),
        }
        for s in ((target.get("tools") or {}).get("schemas") or [])
    ]
    return detail


class CtxDumpPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg: dict = config or {}
        self.include_tools: bool = bool(self.cfg.get("include_tools", True))
        self.keep_latest: bool = bool(self.cfg.get("keep_latest", True))
        self.retention_days: int = int(self.cfg.get("retention_days", 7) or 0)
        self.redact: bool = bool(self.cfg.get("redact", True))
        self.local_server: bool = bool(self.cfg.get("local_server", True))
        self.local_bind: str = str(self.cfg.get("local_bind") or "127.0.0.1")
        self.local_port: int = int(self.cfg.get("local_port", 6199) or 0)

        try:
            self.dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            self.dir = Path("data/plugin_data") / PLUGIN_NAME
        self.dir.mkdir(parents=True, exist_ok=True)
        self.views_dir: Path = Path(__file__).resolve().parent / "views" / "context"

        self._lock = asyncio.Lock()
        self._seq = 0
        self._last_record: dict | None = None
        self._cleaned = False

        self._httpd: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None

        context.register_web_api(
            f"/{PLUGIN_NAME}/sessions", self.api_sessions, ["GET"], "上下文的会话列表"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/list", self.api_list, ["GET"], "时间线数据"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/detail", self.api_detail, ["GET"], "单次请求明细"
        )

        if self.local_server and self.local_port > 0:
            self._start_local_server()

        logger.info(
            f"[ctx_dump] 落盘目录: {self.dir}"
            + (f"；本地页面: http://{self.local_bind}:{self.local_port}" if self._httpd else "")
        )

    # ------------------------------------------------------------------ 本地页面
    def _start_local_server(self) -> None:
        plugin = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CtxDump/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
                return

            def _json(self, payload: Any, status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _static(self, relative: str) -> None:
                root = plugin.views_dir.resolve()
                target = (root / relative).resolve()
                if not str(target).startswith(str(root)) or not target.is_file():
                    self.send_error(404, "Not Found")
                    return
                ctype = {
                    ".html": "text/html; charset=utf-8",
                    ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".svg": "image/svg+xml",
                }.get(target.suffix.lower(), "application/octet-stream")
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                try:
                    if path.startswith("/api/"):
                        name = path[len("/api/") :].strip("/")
                        date = query.get("date") or _today()
                        if not _DATE_RE.match(date):
                            return self._json({"status": "error", "message": "date 需为 YYYY-MM-DD"}, 400)
                        records = read_day(plugin.dir, date)
                        if name == "sessions":
                            payload = {"date": date, **build_sessions(records)}
                        elif name == "list":
                            payload = {
                                "date": date,
                                **build_list(
                                    records,
                                    query.get("umo", ""),
                                    int(query.get("limit", 400) or 400),
                                ),
                            }
                        elif name == "detail":
                            step = build_detail(
                                records, int(query.get("seq", 0) or 0), bool(int(query.get("full", 0) or 0))
                            )
                            if step is None:
                                return self._json({"status": "error", "message": "找不到该 seq"}, 404)
                            payload = {"date": date, "step": step}
                        else:
                            return self._json({"status": "error", "message": "未知接口"}, 404)
                        return self._json(payload)

                    if path in ("/", "/index.html"):
                        return self._static("index.html")
                    return self._static(path.lstrip("/"))
                except BrokenPipeError:
                    return
                except Exception as exc:  # 不让页面把插件带崩
                    logger.exception("[ctx_dump] 本地接口异常")
                    return self._json({"status": "error", "message": str(exc)}, 500)

        try:
            httpd = ThreadingHTTPServer((self.local_bind, self.local_port), Handler)
            httpd.daemon_threads = True
        except OSError as exc:
            logger.warning(
                f"[ctx_dump] 本地页面启动失败（端口 {self.local_port} 可能被占用）: {exc}"
            )
            return

        thread = threading.Thread(
            target=httpd.serve_forever, name="ctx_dump_http", daemon=True
        )
        thread.start()
        self._httpd = httpd
        self._http_thread = thread

    def _stop_local_server(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            logger.warning("[ctx_dump] 本地页面关闭失败", exc_info=True)
        self._http_thread = None

    # ------------------------------------------------------------------ 落盘
    def _mask(self, text: str) -> str:
        if not self.redact or not text:
            return text

        def _sub(m: re.Match) -> str:
            if m.group(1):
                return f"{m.group(1)}***"
            return f"{m.group(2)}{m.group(3)[:3]}***\""

        return _SECRET_RE.sub(_sub, text)

    def _cleanup_old_files(self) -> None:
        if self._cleaned or self.retention_days <= 0:
            return
        self._cleaned = True
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        for f in self.dir.glob("*.jsonl"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink()
            except Exception:
                logger.warning(f"[ctx_dump] 清理失败: {f}")

    def _write_line(self, path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")

    def _write_latest(self, record: dict) -> None:
        with (self.dir / "latest.json").open("w", encoding="utf-8") as fp:
            json.dump(record, fp, ensure_ascii=False, indent=2, default=str)

    async def _append(self, record: dict) -> None:
        if not self._cleaned:
            await asyncio.to_thread(self._cleanup_old_files)
        day = datetime.now().strftime("%Y-%m-%d")
        line = json.dumps(record, ensure_ascii=False, default=str)
        async with self._lock:
            await asyncio.to_thread(self._write_line, self.dir / f"{day}.jsonl", line)
            if self.keep_latest:
                await asyncio.to_thread(self._write_latest, record)

    # ------------------------------------------------------------------ 钩子
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """请求发出前拦截：此时 req 已被框架填好，provider 还没加工。"""
        try:
            self._seq += 1
            system_prompt = self._mask(req.system_prompt or "")
            prompt = self._mask(req.prompt or "")
            contexts = _safe(req.contexts or [])
            if self.redact:
                contexts = json.loads(self._mask(json.dumps(contexts, ensure_ascii=False)))

            trace_id = ""
            trace = getattr(event, "trace", None)
            if trace is not None:
                trace_id = getattr(trace, "trace_id", "") or ""
            try:
                platform = event.get_platform_name()
            except Exception:
                platform = ""
            try:
                sender = event.get_sender_name()
            except Exception:
                sender = ""

            record = {
                "ts": time.time(),
                "iso": _iso_now(),
                "seq": self._seq,
                "trace_id": trace_id,
                "umo": getattr(event, "unified_msg_origin", ""),
                "platform": platform,
                "sender": sender,
                "session_id": req.session_id,
                "model": req.model,
                "system_prompt": system_prompt,
                "prompt": prompt,
                "n_contexts": len(contexts),
                "contexts": contexts,
                "images": _safe(req.image_urls or []),
                "audios": _safe(req.audio_urls or []),
                "tools": _dump_tools(req.func_tool) if self.include_tools else {"names": []},
                "chars": len(system_prompt)
                + len(prompt)
                + len(json.dumps(contexts, ensure_ascii=False)),
            }
            self._last_record = record
            await self._append(record)
        except Exception:
            logger.exception("[ctx_dump] 记录 LLM 请求失败")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        try:
            self._seq += 1
            raw = getattr(resp, "raw_completion", None)
            record = {
                "ts": time.time(),
                "iso": _iso_now(),
                "seq": self._seq,
                "kind": "response",
                "trace_id": getattr(getattr(event, "trace", None), "trace_id", ""),
                "umo": getattr(event, "unified_msg_origin", ""),
                "completion_text": self._mask(getattr(resp, "completion_text", "") or ""),
                "tool_calls": list(getattr(resp, "tools_call_name", []) or []),
                "usage": _safe(getattr(raw, "usage", None)),
            }
            await self._append(record)
        except Exception:
            logger.exception("[ctx_dump] 记录 LLM 响应失败")

    # ------------------------------------------------------------------ Web API
    async def api_sessions(self):
        date = request.query.get("date", _today())
        if not _DATE_RE.match(date):
            return error_response("date 需为 YYYY-MM-DD")
        records = await asyncio.to_thread(read_day, self.dir, date)
        return json_response({"date": date, **build_sessions(records)})

    async def api_list(self):
        date = request.query.get("date", _today())
        if not _DATE_RE.match(date):
            return error_response("date 需为 YYYY-MM-DD")
        umo = request.query.get("umo", "")
        limit = request.query.get("limit", 400, type=int)
        records = await asyncio.to_thread(read_day, self.dir, date)
        return json_response({"date": date, **build_list(records, umo, limit)})

    async def api_detail(self):
        date = request.query.get("date", _today())
        if not _DATE_RE.match(date):
            return error_response("date 需为 YYYY-MM-DD")
        seq = request.query.get("seq", 0, type=int)
        full = bool(request.query.get("full", 0, type=int))
        records = await asyncio.to_thread(read_day, self.dir, date)
        step = build_detail(records, seq, full)
        if step is None:
            return error_response("找不到该 seq", status_code=404)
        return json_response({"date": date, "step": step})

    # ------------------------------------------------------------------ 指令
    @filter.command("ctxdump_last")
    async def cmd_last(self, event: AstrMessageEvent, n: int = 3):
        """最近一次请求的概要。"""
        rec = self._last_record
        if rec is None:
            path = self.dir / "latest.json"
            if path.exists():
                rec = json.loads(path.read_text(encoding="utf-8"))
        if not rec:
            yield event.plain_result("还没有记录到任何 LLM 请求。")
            return
        b = _breakdown(rec)
        head = [
            f"trace_id: {rec.get('trace_id', '')}",
            f"会话: {rec.get('umo', '')}",
            f"模型: {rec.get('model')}",
            f"上下文 {rec.get('n_contexts')} 条 / 估算 {b['total']} tokens",
            "  其中 " + "、".join(f"{k}:{b[k]}" for k in (*CATS, "prompt") if b.get(k)),
            "",
            "=== system_prompt ===",
            (rec.get("system_prompt") or "")[:1200],
            "",
            f"=== 最后 {n} 条 context ===",
        ]
        for msg in (rec.get("contexts") or [])[-max(0, n) :]:
            role = msg.get("role", "?") if isinstance(msg, dict) else "?"
            head.append(f"[{role}] {_msg_text(msg)[:800]}")
        text = "\n".join(head)
        for i in range(0, len(text), 1500):
            yield event.plain_result(text[i : i + 1500])

    @filter.command("ctxdump_stats")
    async def cmd_stats(self, event: AstrMessageEvent):
        """今天的落盘统计。"""
        day = _today()
        records = await asyncio.to_thread(read_day, self.dir, day)
        reqs = [r for r in records if r.get("kind") != "response"]
        if not reqs:
            yield event.plain_result("今天还没有记录。")
            return
        tokens = [_breakdown(r)["total"] for r in reqs]
        max_ctx = max(int(r.get("n_contexts") or 0) for r in reqs)
        yield event.plain_result(
            f"日期 {day}\n请求 {len(reqs)} 条 / 响应 {len(records) - len(reqs)} 条\n"
            f"contexts 最多 {max_ctx} 条，估算 token 合计 {sum(tokens)}，单次最大 {max(tokens)}\n"
            f"文件: {self.dir / (day + '.jsonl')}"
        )

    @filter.command("ctxdump_path")
    async def cmd_path(self, event: AstrMessageEvent):
        """返回落盘目录和本地页面地址。"""
        lines = [f"上下文落盘目录：{self.dir}"]
        if self._httpd:
            lines.append(f"本地页面：http://{self.local_bind}:{self.local_port}")
        yield event.plain_result("\n".join(lines))

    @filter.command("ctxdump_purge")
    async def cmd_purge(self, event: AstrMessageEvent, confirm: str = ""):
        """删除全部已落盘的上下文（需要 /ctxdump_purge yes 确认）。"""
        if confirm != "yes":
            yield event.plain_result(
                "这会删除全部 JSONL 记录，不可恢复。确认请输入 /ctxdump_purge yes"
            )
            return
        removed = 0
        for f in self.dir.glob("*.jsonl"):
            try:
                f.unlink()
                removed += 1
            except Exception:
                logger.warning(f"[ctx_dump] 删除失败: {f}")
        latest = self.dir / "latest.json"
        if latest.exists():
            try:
                latest.unlink()
            except Exception:
                pass
        yield event.plain_result(f"已删除 {removed} 个记录文件。")

    async def terminate(self):
        self._stop_local_server()
        logger.info("[ctx_dump] 插件已卸载，本地页面已关闭")
