# -*- coding: utf-8 -*-
"""飞书审核机器人（交互卡片版）

用法:
  python bot.py            前台常驻运行，Ctrl+C 退出（机器人即在线）

飞书里的交互流程:
  单聊机器人发送「审核」
   -> 机器人回复选表卡片（勾选工作表，可多选）
   -> 点「🚀 开始审核」或「👀 仅预览」
   -> 卡片实时刷新审核进度
   -> 审核完成后卡片展示结果明细
   -> 正式模式下点「✔ 确认回写」写入飞书表格

前置条件（飞书开放平台 > 应用 cli_aa17a3139ab8dcc0）:
  1. 添加应用能力「机器人」
  2. 权限: im:message:send_as_bot（发消息）、im:message.p2p_msg:readonly（收单聊消息）、
     im:message:patch（更新已发送的消息卡片）
  3. 事件与回调: 订阅方式选「使用长连接接收事件」，
     添加事件「接收消息 im.message.receive_v1」和「卡片回传交互回调 card.action.trigger」
  4. 版本管理与发布: 创建版本并发布
"""
import asyncio
import json
import os
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from lark_oapi.channel import FeishuChannel

from fsaudit.config import load_config
from fsaudit.engine import AuditEngine

PROGRESS_INTERVAL = 2.5      # 审核中卡片刷新间隔（秒）
MAX_RESULT_ROWS = 30         # 结果卡片最多列出的文档数
SHEETS_CACHE_TTL = 60        # 工作表列表缓存秒数
# 机器人启动时主动推送「审核工作台」卡片的单聊会话（你与机器人的 p2p chat_id）
WORKBENCH_CHAT_ID = "oc_e7f67309de0261d6d0a581b277c7d0d0"
# 已推送的工作台卡片 message_id 持久化文件（重启去重用：重启=原位刷新旧卡，不重复推送）
WORKBENCH_STATE_FILE = os.path.join(BASE_DIR, "workbench_state.json")

cfg = load_config()
engine = AuditEngine(cfg)
channel = FeishuChannel(app_id=cfg.app_id, app_secret=cfg.app_secret)

HELP_TEXT = (
    "🤖 审核机器人用法\n"
    "「审核」— 发送选表卡片，勾选工作表后开始\n"
    "卡片按钮：\n"
    "  🚀 开始审核 — 审核完成后需点「确认回写」才写入表格\n"
    "  👀 仅预览 — 只看结果，不写入\n"
    "说明：\n"
    "  · 只审「回链」列挂 .doc/.docx 附件的行，其他自动跳过\n"
    "  · 「机器审核」列已有值的行自动跳过（增量审核）\n"
    "  · 日志：audit_YYYYMMDD.log"
)

_lock = threading.Lock()
state = {
    "phase": "idle",          # idle / auditing / awaiting
    "chat_id": None,
    "card_id": None,          # 当前进度/结果卡片的 message_id
    "run": None,              # engine.run() 的返回值
    "dry_run": False,
    "results": [],            # 审核中渐进结果
    "current": None,          # {"sheet", "index", "total", "file"}
    "logs": [],
}
_sheets_cache = {"ts": 0.0, "data": []}


# ---------------------------------------------------------------- 卡片构造

def _pt(content):
    return {"tag": "plain_text", "content": content}


def _btn(content, value, style="default"):
    """普通回调按钮（卡片 JSON 2.0：behaviors.callback，兼容历史 value）"""
    return {"tag": "button", "text": _pt(content), "type": style,
            "behaviors": [{"type": "callback", "value": value}],
            "value": value}


def _form_btn(content, value, name, style="default"):
    """表单容器内提交按钮（JSON 2.0：name + form_action_type=submit；
    回调数据经 behaviors.callback 回传 value）"""
    return {"tag": "button", "text": _pt(content), "type": style,
            "name": name, "form_action_type": "submit",
            "behaviors": [{"type": "callback", "value": value}],
            "value": value}


def picker_card(sheets, workbench=False):
    usable = [s for s in sheets if s["usable"]]
    other = [s for s in sheets if not s["usable"]]
    if not usable:
        return {"schema": "2.0", "config": {"enable_forward": False},
                "header": {"title": _pt("⚠ 没有可审核的工作表"), "template": "red"},
                "body": {"elements": [{"tag": "markdown",
                                       "content": "没有找到同时含「项目」和「回链」列的工作表，请检查表格或 config.yaml 的列配置。"}]}}
    if workbench:
        md = ("**审核工作台** — 下拉选择工作表，再点下方按钮开始\n"
              "只审回链挂 .doc/.docx 附件的行，「机器审核」列已有值的行自动跳过。\n\n"
              "📌 本卡**常驻**：审核进度与结果会显示在**下方新卡片**，不会占用本卡；"
              "审完一轮直接回来重新勾选即可，无需重启机器人。")
    else:
        md = ("**选择要审核的工作表**（可多选）\n"
              "只审回链挂 .doc/.docx 附件的行，「机器审核」列已有值的行自动跳过。")
    if other:
        md += "\n\n<font color='grey'>不可审核（缺列）：" + \
              "、".join(s["title"] for s in other) + "</font>"
    options = [{"text": _pt(s["title"]), "value": s["sheet_id"]} for s in usable]
    elements = [
        {"tag": "markdown", "content": md},
        {"tag": "form", "name": "audit_form", "elements": [
            {"tag": "multi_select_static", "name": "sheets",
             "placeholder": _pt("点击选择工作表（可多选）"),
             "options": options},
            _form_btn("🚀 开始审核", {"cmd": "start"}, "btn_start", "primary"),
            _form_btn("👀 仅预览（不回写）", {"cmd": "preview"}, "btn_preview", "default"),
        ]},
    ]
    if workbench:
        elements.append({"tag": "markdown",
                         "content": "<font color='grey'>工作表有增删时点「🔄 刷新」更新列表；本卡片常驻，随时可用。</font>"})
        elements.append(_btn("🔄 刷新工作表", {"cmd": "refresh"}, "default"))
    else:
        elements.append({"tag": "markdown",
                         "content": "<font color='grey'>开始审核 = 审核后需确认回写；仅预览 = 只看结果不写入</font>"})
    return {"schema": "2.0", "config": {"enable_forward": False},
            "header": {"title": _pt("📋 审核工作台" if workbench else "📋 文章审核"),
                       "template": "blue"},
            "body": {"elements": elements}}


def progress_card():
    with _lock:
        cur = state["current"]
        results = list(state["results"])
        logs = list(state["logs"][-3:])
    lines = []
    if cur:
        lines.append("⏳ 正在审核 **%s**（第 %d / %d 个文档：%s）"
                     % (cur.get("sheet", ""), cur.get("index", 0),
                        cur.get("total", 0), (cur.get("file") or "")[:50]))
    else:
        lines.append("⏳ 正在读取表格…")
    npass = sum(1 for r in results if r["passed"])
    nfail = len(results) - npass
    lines.append("\n✅ 通过 %d · ❌ 不通过 %d" % (npass, nfail))
    for r in results[-5:]:
        icon = "✅" if r["passed"] else "❌"
        lines.append("- %s 行%d %s" % (icon, r["row"], (r["file"] or "")[:40]))
    for lg in logs:
        lines.append("<font color='grey'>%s</font>" % lg)
    elements = [{"tag": "markdown", "content": "\n".join(lines)}]
    return {"schema": "2.0", "config": {"enable_forward": False},
            "header": {"title": _pt("⏳ 审核进行中"), "template": "turquoise"},
            "body": {"elements": elements}}


def _results_md(run):
    results = run["results"]
    if not results:
        return "没有待审文档（已全部审过，或勾选的表里没有 .doc/.docx 附件行）。"
    by_sheet = {}
    for r in results:
        by_sheet.setdefault(r.get("sheet", ""), []).append(r)
    lines, n = [], 0
    for sheet, rows in by_sheet.items():
        lines.append("**%s**" % sheet)
        for r in rows:
            if n >= MAX_RESULT_ROWS:
                lines.append("<font color='grey'>…其余 %d 行略（详见日志）</font>"
                             % (len(results) - n))
                return "\n".join(lines)
            icon = "✅" if r["passed"] else "❌"
            line = "- %s 行%d %s" % (icon, r["row"], (r["file"] or "")[:40])
            if not r["passed"] and r["reasons"]:
                line += "：" + "；".join(r["reasons"])[:150]
            lines.append(line)
            n += 1
    return "\n".join(lines)


def result_card():
    with _lock:
        run = state["run"]
        dry = run.get("dry_run", state["dry_run"])
    st = run["stat"]
    npass, nfail = st["pass"], st["fail"]
    summary = ("✅ 通过 %d · ❌ 不通过 %d · ↩ 已审跳过 %d · ⏭ 非文档跳过 %d · ⚠ 失败 %d"
               % (npass, nfail, st["skipped"], st["nondoc"], st["error"]))
    elements = [{"tag": "markdown", "content": summary}, {"tag": "hr"},
                 {"tag": "markdown", "content": _results_md(run)}]

    if dry:
        title, template = "👀 预览完成（未回写）", "orange"
        elements.append({"tag": "markdown",
                         "content": "<font color='grey'>仅预览模式：结果未写入表格。需要回写请重新「审核」并点「开始审核」。</font>"})
    elif run["writebacks"]:
        title, template = "审核完成，待确认回写", "blue"
        elements.append({"tag": "hr"})
        elements.append({"tag": "column_set", "columns": [
            {"tag": "column", "elements": [
                _btn("✔ 确认回写飞书表格", {"cmd": "confirm"}, "primary")]},
            {"tag": "column", "elements": [
                _btn("✖ 放弃不写", {"cmd": "discard"}, "default")]},
        ]})
    else:
        title, template = "审核完成", "blue"
        elements.append({"tag": "markdown",
                         "content": "<font color='grey'>没有需要回写的结果。</font>"})
    # 统一操作指引：工作台卡常驻，下一轮无需重启机器人
    elements.append({"tag": "markdown",
                     "content": "<font color='grey'>🔄 再来一轮？直接点本会话上方「📋 审核工作台」卡片重新勾选即可。</font>"})
    if nfail > 0 and npass == 0:
        template = "red"
    return {"schema": "2.0", "config": {"enable_forward": False},
            "header": {"title": _pt(title), "template": template},
            "body": {"elements": elements}}


def written_card(res):
    ok, failures = res["ok"], res["failures"]
    elements = [{"tag": "markdown",
                 "content": "✔ 已回写飞书表格 **%d** 行（机器审核 / 不通过原因列）。" % ok}]
    if failures:
        elements.append({"tag": "markdown",
                         "content": "❌ 回写失败 %d 行，例如 [%s 行%d]：%s\n提示：请确认应用对表格有「可编辑」权限。"
                                    % (len(failures), failures[0]["sheet"],
                                       failures[0]["row"], failures[0]["error"])})
    elements.append({"tag": "markdown",
                     "content": "<font color='grey'>🔄 再来一轮？直接点本会话上方「📋 审核工作台」卡片重新勾选即可。</font>"})
    return {"schema": "2.0", "config": {"enable_forward": False},
            "header": {"title": _pt("✔ 回写完成"), "template": "green"},
            "body": {"elements": elements}}


# ---------------------------------------------------------------- 线程工具

def update_card_sync(message_id, card):
    """在工作线程里把卡片更新请求安全地投递到 channel 的事件循环"""
    loop = channel._bg_loop
    if loop is None or loop.is_closed():
        return
    fut = asyncio.run_coroutine_threadsafe(channel.update_card(message_id, card), loop)
    fut.result(timeout=60)


def get_sheets(force=False):
    now = time.time()
    if not force and _sheets_cache["data"] and now - _sheets_cache["ts"] < SHEETS_CACHE_TTL:
        return _sheets_cache["data"]
    data = engine.list_sheets()
    _sheets_cache["ts"], _sheets_cache["data"] = now, data
    return data


# ---------------------------------------------------------------- 审核执行

def start_audit(chat_id, card_id, sheet_ids, dry_run):
    last = [0.0]

    def emit(ev):
        with _lock:
            if ev["type"] == "row_start":
                state["current"] = ev
            elif ev["type"] == "row_done":
                state["results"].append(ev)
            elif ev["type"] == "log":
                state["logs"].append(ev["msg"])
        if ev["type"] == "row_done" and time.time() - last[0] >= PROGRESS_INTERVAL:
            last[0] = time.time()
            try:
                update_card_sync(card_id, progress_card())
            except Exception as e:
                print("[progress-update-failed] %s" % e)

    try:
        run = engine.run(sheet_ids, dry_run=dry_run, force=False, on_event=emit)
    except Exception as e:
        try:
            update_card_sync(card_id, {"schema": "2.0",
                                       "config": {"enable_forward": False},
                                       "header": {"title": _pt("❌ 审核出错"), "template": "red"},
                                       "body": {"elements": [{"tag": "markdown", "content": str(e)[:800]}]}})
        except Exception:
            pass
        with _lock:
            state["phase"] = "idle"
        return

    with _lock:
        state["run"] = run
        state["dry_run"] = dry_run
        state["phase"] = "awaiting" if (not dry_run and run["writebacks"]) else "idle"
    try:
        update_card_sync(card_id, result_card())
    except Exception as e:
        print("[result-update-failed] %s" % e)


def do_writeback(card_id):
    with _lock:
        run = state["run"]
    if not run:
        return

    def emit(ev):
        pass  # 回写逐行日志太多，不刷卡片

    res = engine.write_back(run, on_event=emit)
    try:
        update_card_sync(card_id, written_card(res))
    except Exception as e:
        print("[write-card-failed] %s" % e)
    with _lock:
        state["phase"] = "idle"
        state["run"] = None


# ---------------------------------------------------------------- 事件处理

def _form_value(evt):
    raw = evt.raw or {}
    try:
        action = (raw.get("event") or {}).get("action") or {}
        return action.get("form_value") or {}
    except Exception:
        return {}


async def on_message(msg):
    try:
        print("[message] chat=%s type=%s text=%r" % (msg.chat_id, msg.chat_type, (msg.content_text or "")[:60]), flush=True)
        if msg.chat_type != "p2p" and not msg.mentioned_bot:
            return
        text = (msg.content_text or "").strip()
        if not text:
            return
        if "帮助" in text or "help" in text.lower():
            await channel.send(msg.chat_id, {"text": HELP_TEXT})
            return
        if "审核" in text or "菜单" in text or "开始" in text or "选表" in text:
            card = picker_card(get_sheets())
            await channel.send(msg.chat_id, {"card": card})
            return
        await channel.send(msg.chat_id, {"text": "发送「审核」发起一次文章审核，发送「帮助」查看用法。"})
    except Exception as e:
        print("[on-message-error] %s" % e)


async def on_card(evt):
    try:
        value = evt.action.value if isinstance(evt.action.value, dict) else {}
        cmd = value.get("cmd")
        fv = _form_value(evt)
        print("[card-action] chat=%s card=%s cmd=%s value=%r fv=%r"
              % (evt.chat_id, evt.message_id, cmd, value, fv))

        if cmd in ("start", "preview"):
            with _lock:
                busy = state["phase"] != "idle"
            if busy:
                await channel.send(evt.chat_id, {"text": "⏳ 当前已有审核任务进行中，请等它完成后再发起。"})
                return
            fv = _form_value(evt)
            sheet_ids = fv.get("sheets") or []
            if isinstance(sheet_ids, str):
                sheet_ids = [sheet_ids]
            if not sheet_ids:
                # 没勾选任何工作表：明确提示，避免"点了没反应"
                try:
                    await channel.send(evt.chat_id, {
                        "text": "⚠️ 请先点击卡片里的「选择工作表」下拉框，勾选要审核的工作表（可多选）后再点按钮。"})
                except Exception as e:
                    print("[empty-hint-failed] %s" % e)
                return
            with _lock:
                state.update(phase="auditing", chat_id=evt.chat_id, card_id=evt.message_id,
                             run=None, dry_run=(cmd == "preview"), results=[], current=None, logs=[])
            dry_run = (cmd == "preview")

            # 新建一张独立的「审核会话卡」承载进度与结果；工作台卡保持常驻，不被顶掉。
            # 这样一轮审完可直接回工作台卡点下一轮，无需重启机器人。
            session_id = evt.message_id
            try:
                res = await channel.send(evt.chat_id, {"card": progress_card()})
                if res.success and res.message_id:
                    session_id = res.message_id
                    with _lock:
                        state["card_id"] = session_id
                    print("[audit-session] 已新建审核会话卡 %s（工作台卡保持常驻）" % session_id, flush=True)
                else:
                    print("[audit-session] 新建会话卡失败(%s)，退回原位更新" % (res.error or "?"), flush=True)
            except Exception as e:
                print("[audit-session] 新建会话卡异常 %s，退回原位更新" % e, flush=True)

            threading.Thread(target=start_audit,
                             args=(evt.chat_id, session_id, sheet_ids, dry_run),
                             daemon=True).start()
            return

        if cmd == "refresh":
            try:
                update_card_sync(evt.message_id, picker_card(get_sheets(force=True), workbench=True))
            except Exception as e:
                print("[refresh-failed] %s" % e)
            return

        if cmd == "confirm":
            with _lock:
                ok = state["phase"] == "awaiting" and state["run"]
            if not ok:
                return
            with _lock:
                state["phase"] = "writing"
            threading.Thread(target=do_writeback, args=(evt.message_id,), daemon=True).start()
            return

        if cmd == "discard":
            with _lock:
                state["phase"] = "idle"
                state["run"] = None
            try:
                update_card_sync(evt.message_id, {"schema": "2.0",
                                                   "config": {"enable_forward": False},
                                                   "header": {"title": _pt("已放弃"), "template": "grey"},
                                                   "body": {"elements": [{"tag": "markdown",
                                                                         "content": "已放弃本次结果，未写入表格。可在上方工作台卡片重新勾选开始。"}]}})
            except Exception:
                pass
            return

        # 未知/自测命令（如链路验证卡的 cmd=test）：回执确认回调已到达
        try:
            update_card_sync(evt.message_id, {"schema": "2.0",
                                              "config": {"enable_forward": False},
                                              "header": {"title": _pt("✅ 回调链路正常"), "template": "green"},
                                              "body": {"elements": [{"tag": "markdown",
                                                                    "content": "已收到按钮回调（cmd=`%s`）。现在可以给机器人发「审核」正式使用。" % cmd}]}})
        except Exception as e:
            print("[card-ack-failed] %s" % e)
        return
    except Exception as e:
        print("[on-card-error] %s" % e)


def _load_wb_state():
    try:
        with open(WORKBENCH_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_wb_state(mid):
    try:
        with open(WORKBENCH_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"message_id": mid, "ts": time.time()}, f, ensure_ascii=False)
    except Exception as e:
        print("[workbench] 状态保存失败: %s" % e, flush=True)


def _submit(loop, coro_fn, *args):
    """把协程提交到 channel 事件循环并同步等待结果"""
    fut = asyncio.run_coroutine_threadsafe(coro_fn(*args), loop)
    return fut.result(timeout=30)


def _try_pin_workbench(loop, mid):
    """把常驻工作台卡「置顶」到会话顶部固定区（飞书 pin）。

    置顶后工作台卡从普通消息流中抽出，固定在会话最上方独立区域，
    无论下面来多少条审核结果卡都不会把它顶走/淹没。
    置顶失败不阻塞主流程，仅打日志（多为应用缺 im:message:pin 权限或版本未发布）。
    """
    try:
        from lark_oapi.api.im.v1.model import CreatePinRequest, CreatePinRequestBody
    except Exception as e:
        print("[workbench] 导入 pin 模型失败(忽略): %s" % e, flush=True)
        return

    async def _pin():
        req = (CreatePinRequest.builder()
               .request_body(CreatePinRequestBody.builder().message_id(mid).build())
               .build())
        resp = await channel._client.im.v1.pin.acreate(req)
        ok = resp.success() if hasattr(resp, "success") else (getattr(resp, "code", -1) == 0)
        if ok:
            print("[workbench] ✅ 已置顶工作台卡 %s（固定于会话顶部）" % mid, flush=True)
        else:
            print("[workbench] 置顶接口返回 code=%s msg=%s" % (resp.code, resp.msg), flush=True)

    try:
        fut = asyncio.run_coroutine_threadsafe(_pin(), loop)
        fut.result(timeout=15)
    except Exception as e:
        print("[workbench] 置顶调用异常(忽略): %s" % e, flush=True)


def send_workbench_sync():
    """把常驻「审核工作台」卡片送到用户单聊（全程按钮驱动，不依赖消息事件）。

    去重策略：本地记录已推送的工作台卡 message_id，
    机器人重启时先尝试原位刷新旧卡（不重复堆卡）；
    旧卡已不存在/刷新失败时才新推一张，并更新本地记录。
    """
    card = picker_card(get_sheets(force=True), workbench=True)
    deadline = time.time() + 60
    while True:
        loop = channel._bg_loop
        if loop is None or loop.is_closed():
            if time.time() > deadline:
                break
            time.sleep(1)
            continue
        # 1) 原位刷新历史工作台卡（重启去重）
        old = _load_wb_state().get("message_id")
        if old:
            try:
                r = _submit(loop, channel.update_card, old, card)
                if r.success:
                    print("[workbench] 已原位刷新常驻工作台卡 %s（未重复推送）" % old, flush=True)
                    _try_pin_workbench(loop, old)
                    return
                print("[workbench] 旧卡刷新返回失败(%s)，改推新卡" % (r.error or "?"), flush=True)
            except Exception as e:
                print("[workbench] 旧工作台卡(%s)刷新异常 %s → 改推新卡" % (old, e), flush=True)
        # 2) 推送新卡（直到成功或超时）
        try:
            res = _submit(loop, channel.send, WORKBENCH_CHAT_ID, {"card": card})
            if res.success and res.message_id:
                _save_wb_state(res.message_id)
                print("[workbench] 已推送审核工作台卡片 %s → %s" % (res.message_id, WORKBENCH_CHAT_ID), flush=True)
                _try_pin_workbench(loop, res.message_id)
                return
            raise RuntimeError(res.error or "send failed")
        except Exception as e:
            if time.time() > deadline:
                print("[workbench] 推送失败(60s 超时): %s" % e, flush=True)
                return
            print("[workbench] 发送重试中: %s" % e, flush=True)
            time.sleep(2)
    print("[workbench] 推送失败(60s 超时)", flush=True)


def main():
    print("=" * 50)
    print("  Feishu Audit Bot")
    print("  spreadsheet token: %s" % cfg.spreadsheet_token)
    print("  LLM: %s" % (cfg.llm.model if cfg.llm.api_key else "(disabled)"))
    print("  connecting via websocket (long connection)...")
    print("=" * 50)
    channel.on("message", on_message)
    channel.on("cardAction", on_card)
    threading.Thread(target=send_workbench_sync, daemon=True).start()
    try:
        channel.start()
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
