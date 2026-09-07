# -*- coding: utf-8 -*-
"""飞书文档审核机器人 · 运维面板后端

职责：
  1. 读写 config.yaml（表格链接 / LLM / 列名映射）
  2. 以子进程方式托管 bot.py：启动 / 停止 / 重启
  3. 提供本地 HTTP API 与静态页面，浏览器 http://127.0.0.1:8788 即可操作

设计约定：
  · 仅监听 127.0.0.1，无鉴权（本机专用）
  · bot.py 本身不改动，仍可独立 `python bot.py` 运行
  · 面板进程退出不杀 bot（让审核服务继续跑）；再次启动面板会自动"认领"残留进程
"""
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from fsaudit.config import load_config, save_config, parse_spreadsheet_token  # noqa: E402

PORT = int(os.environ.get("PANEL_PORT", "8788"))
PID_FILE = os.path.join(BASE_DIR, "bot.pid")
RUNTIME_LOG = os.path.join(BASE_DIR, "bot_runtime.log")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_lock = threading.RLock()   # RLock：bot_status 会被 start/stop 在持锁状态下调用
_proc = None            # 本进程本次会话托管的 bot 子进程
_started_at = None      # 托管启动时刻（epoch）


# ---------------------------------------------------------------- 日志工具

def tail_file(path, n=120):
    """读取文件末尾 n 行（支持任意编码容错）"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        return lines[-n:]
    except Exception:
        return []


# ---------------------------------------------------------------- bot 进程托管

def _pid_alive(pid):
    if not pid:
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
            capture_output=True, text=True, timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        return str(pid) in (r.stdout or "")
    except Exception:
        return False


def _save_pid(pid):
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
    except Exception:
        pass


def _load_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _clear_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def bot_status():
    """返回 {running, pid, managed, started_at}。managed=是否本面板托管实例。"""
    global _proc, _started_at
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return {"running": True, "pid": _proc.pid, "managed": True,
                    "started_at": _started_at}
        pid = _load_pid()
        if pid and _pid_alive(pid):
            return {"running": True, "pid": pid, "managed": False,
                    "started_at": None}
    return {"running": False, "pid": None, "managed": False, "started_at": None}


def bot_start():
    """以子进程方式启动 bot.py；若已有进程在跑则拒绝。"""
    global _proc, _started_at
    with _lock:
        st = bot_status()
        if st["running"]:
            return {"ok": False, "msg": "机器人已在运行（pid=%s）" % st["pid"]}
        logf = open(RUNTIME_LOG, "ab")
        logf.write(("\n[panel %s] 启动 bot.py ...\n" % time.strftime("%H:%M:%S"))
                   .encode("utf-8"))
        logf.flush()
        try:
            _proc = subprocess.Popen(
                [sys.executable, "bot.py"],
                cwd=BASE_DIR,
                stdout=logf, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            logf.close()
            return {"ok": False, "msg": "启动失败: %s" % e}
        _save_pid(_proc.pid)
        _started_at = time.time()
    return {"ok": True, "pid": _proc.pid}


def bot_stop():
    """停止机器人进程（含认领的残留进程）。"""
    global _proc, _started_at
    target = None
    with _lock:
        st = bot_status()
        if not st["running"]:
            return {"ok": True, "msg": "机器人当前未在运行"}
        target = st["pid"]
    try:
        os.kill(target, signal.SIGTERM)   # Windows 下等价 TerminateProcess
    except Exception:
        pass
    for _ in range(10):                   # 最多等 3 秒
        if not _pid_alive(target):
            break
        time.sleep(0.3)
    if _pid_alive(target):
        return {"ok": False, "msg": "停止失败：进程(pid=%s)仍在" % target}
    with _lock:
        _proc = None
        _started_at = None
        _clear_pid()
    try:
        with open(RUNTIME_LOG, "ab") as f:
            f.write(("[panel %s] bot.py 已停止\n" % time.strftime("%H:%M:%S"))
                    .encode("utf-8"))
    except Exception:
        pass
    return {"ok": True, "msg": "已停止（pid=%s）" % target}


def bot_restart():
    r = bot_stop()
    if not r["ok"]:
        return r
    time.sleep(0.5)
    return bot_start()


# ---------------------------------------------------------------- 配置读写

def public_config():
    cfg = load_config()
    return {
        "spreadsheet_token": cfg.spreadsheet_token,
        "folder_token": cfg.folder_token,
        "llm": {
            "base_url": cfg.llm.base_url,
            "model": cfg.llm.model,
            "api_key_set": bool(cfg.llm.api_key),
        },
        "columns": cfg.columns,
        "forbidden_words": cfg.forbidden_words,
    }


def update_config(payload):
    cfg = load_config()
    notes = []

    # 1) 表格链接（支持整段链接或裸 token，留空 = 不改）
    raw = (payload.get("spreadsheet") or "").strip()
    if raw:
        try:
            token = parse_spreadsheet_token(raw)
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        if token != cfg.spreadsheet_token:
            notes.append("表格 token：%s → %s" % (cfg.spreadsheet_token, token))
            cfg.spreadsheet_token = token

    # 2) LLM 配置
    llm = payload.get("llm") or {}
    if (llm.get("base_url") or "").strip():
        cfg.llm.base_url = llm["base_url"].strip()
    if (llm.get("model") or "").strip():
        cfg.llm.model = llm["model"].strip()
    if llm.get("clear_api_key"):
        if cfg.llm.api_key:
            notes.append("已清除 LLM API Key")
        cfg.llm.api_key = ""
    elif (llm.get("api_key") or "").strip():
        cfg.llm.api_key = llm["api_key"].strip()
        notes.append("已更新 LLM API Key")
    if (llm.get("base_url") or llm.get("model")) and not cfg.llm.api_key:
        notes.append("⚠ 当前未配置 API Key，LLM 深度审核不会启用")

    # 3) 列名映射（可选）
    cols = payload.get("columns") or {}
    changed_cols = []
    for k in ("keyword", "link", "result", "reason"):
        v = (cols.get(k) or "").strip()
        if v and v != cfg.columns[k]:
            changed_cols.append("%s: %s→%s" % (k, cfg.columns[k], v))
            cfg.columns[k] = v
    if changed_cols:
        notes.append("列名映射：" + "；".join(changed_cols))

    # 4) 违禁词（可选，逗号分隔）
    fw = payload.get("forbidden_words")
    if isinstance(fw, str) and fw.strip():
        lst = [x.strip() for x in re.split(r"[,，]", fw) if x.strip()]
        if lst != cfg.forbidden_words:
            cfg.forbidden_words = lst
            notes.append("违禁词：%s" % "、".join(lst))

    try:
        save_config(cfg)
    except Exception as e:
        return {"ok": False, "msg": "保存 config.yaml 失败: %s" % e}
    notes = notes or ["配置无变化，未写入"]
    return {"ok": True, "msg": "；".join(notes),
            "spreadsheet_token": cfg.spreadsheet_token}


def verify_sheet():
    """用当前配置 token 实际连一次飞书，列出工作表，验证授权与列结构。"""
    cfg = load_config()
    try:
        from fsaudit.engine import AuditEngine
        eng = AuditEngine(cfg=cfg)
        t0 = time.time()
        sheets = eng.list_sheets()
        usable = [s for s in sheets if s["usable"]]
        return {
            "ok": True,
            "token": cfg.spreadsheet_token,
            "cost_ms": int((time.time() - t0) * 1000),
            "total": len(sheets),
            "usable": len(usable),
            "sheets": [{"title": s["title"], "usable": s["usable"]}
                       for s in sheets[:40]],
        }
    except Exception as e:
        return {"ok": False, "token": cfg.spreadsheet_token, "msg": str(e)}


# ---------------------------------------------------------------- HTTP 服务

def _send_json(h, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    server_version = "FeishuAuditPanel/1.0"

    def log_message(self, fmt, *args):   # 静默访问日志
        return

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/panel"):
            self._serve_static()
        elif path == "/api/state":
            cfg = load_config()
            st = bot_status()
            llm = cfg.llm
            _send_json(self, 200, {
                "ok": True,
                "bot": st,
                "config": public_config(),
                "llm": {"model": llm.model if llm.api_key else
                        "%s (未配Key，已停用)" % llm.model,
                        "api_key_set": bool(llm.api_key)},
            })
        elif path == "/api/log":
            q = self._qs()
            src = q.get("src", ["runtime"])[0]
            n = int(q.get("n", ["120"])[0])
            if src == "audit":
                today = time.strftime("%Y%m%d")
                f = os.path.join(BASE_DIR, "audit_%s.log" % today)
            else:
                f = RUNTIME_LOG
            lines = tail_file(f, n) or ["（暂无日志）"]
            _send_json(self, 200, {"ok": True, "lines": lines})
        else:
            _send_json(self, 404, {"ok": False, "msg": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
        except Exception:
            payload = {}

        if path == "/api/config":
            _send_json(self, 200, update_config(payload))
        elif path == "/api/verify":
            _send_json(self, 200, verify_sheet())
        elif path == "/api/bot":
            act = payload.get("action")
            fn = {"start": bot_start, "stop": bot_stop,
                  "restart": bot_restart}.get(act)
            if not fn:
                _send_json(self, 400, {"ok": False, "msg": "未知 action"})
            else:
                _send_json(self, 200, fn())
        else:
            _send_json(self, 404, {"ok": False, "msg": "not found"})

    # ---- 辅助 ----
    def _qs(self):
        from urllib.parse import parse_qs
        return parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}

    def _serve_static(self):
        f = os.path.join(BASE_DIR, "panel", "index.html")
        try:
            with open(f, "rb") as fp:
                body = fp.read()
        except Exception:
            _send_json(self, 500, {"ok": False, "msg": "panel/index.html 缺失"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    # 启动前先"认领"可能残留的旧 bot（面板重启、bot 仍在跑的场景）
    st = bot_status()
    if st["running"]:
        print("[panel] 检测到 bot 已在运行 pid=%s（由面板认领管理）" % st["pid"], flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("[panel] 运维面板已启动:  http://127.0.0.1:%d" % PORT, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
