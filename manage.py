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

from fsaudit.config import load_config, save_config, parse_spreadsheet_token, parse_folder_token  # noqa: E402

PORT = int(os.environ.get("PANEL_PORT", "8788"))
HOST = os.environ.get("PANEL_HOST", "127.0.0.1")    # 监听地址；上云部署时改为 0.0.0.0
PANEL_USER = os.environ.get("PANEL_USER", "")        # Basic Auth 用户名；空=不强制
PANEL_PASS = os.environ.get("PANEL_PASS", "")        # Basic Auth 密码；空=不强制
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
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                capture_output=True, text=True, timeout=8,
                errors="replace",   # 中文 Windows 下 tasklist 输出含非 UTF-8 字节，
                                    # 不加会触发 UnicodeDecodeError 让 reader 线程裸崩
                creationflags=CREATE_NO_WINDOW,
            )
            return str(pid) in (r.stdout or "")
        except Exception:
            return False
    # POSIX（Linux/macOS）：kill -0 检查 + /proc 兜底
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # 进程存在，只是不归我们管
    except OSError:
        return False
    try:
        os.stat("/proc/%d" % pid)
        return True
    except OSError:
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
            popen_kwargs = dict(stdout=logf, stderr=subprocess.STDOUT)
            if os.name == "nt":
                popen_kwargs["creationflags"] = CREATE_NO_WINDOW
            _proc = subprocess.Popen(
                [sys.executable, "bot.py"],
                cwd=BASE_DIR,
                **popen_kwargs,
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
        "app_id": cfg.app_id,
        "app_secret_set": bool(cfg.app_secret),
        "spreadsheet_token": cfg.spreadsheet_token,
        "folder_token": cfg.folder_token,
        "llm": {
            "base_url": cfg.llm.base_url,
            "model": cfg.llm.model,
            "api_key_set": bool(cfg.llm.api_key),
        },
        "columns": cfg.columns,
        "forbidden_words": cfg.forbidden_words,
        "rules_enabled": cfg.rules_enabled,
        "concurrency": cfg.concurrency,
    }


def update_config(payload):
    cfg = load_config()
    notes = []

    # 0) 机器人应用凭证（app_id / app_secret；留空 = 不改）
    app = payload.get("app") or {}
    cred_changed = False
    new_app_id = (app.get("app_id") or "").strip()
    if new_app_id and new_app_id != cfg.app_id:
        if not re.fullmatch(r"cli_[A-Za-z0-9]+", new_app_id):
            return {"ok": False, "msg": "app_id 格式不对，应为 cli_ 开头的字符串（飞书开放平台→应用详情页）"}
        notes.append("应用 app_id：%s → %s" % (cfg.app_id or "(空)", new_app_id))
        cfg.app_id = new_app_id
        cred_changed = True
    if app.get("clear_app_secret"):
        if cfg.app_secret:
            notes.append("已清除 App Secret")
        cfg.app_secret = ""
        cred_changed = True
    elif (app.get("app_secret") or "").strip():
        cfg.app_secret = app["app_secret"].strip()
        notes.append("已更新 App Secret")
        cred_changed = True
    if cred_changed and not (cfg.app_id and cfg.app_secret):
        notes.append("⚠ 应用凭证不完整（app_id / App Secret 缺一），机器人将无法连接飞书")

    # 0.5) 云空间文件夹 token（可选；支持整段链接；可清除）
    raw = (payload.get("folder") or "").strip()
    if app.get("clear_folder") or payload.get("clear_folder"):
        if cfg.folder_token:
            notes.append("已清除云空间文件夹 token")
        cfg.folder_token = ""
    elif raw:
        try:
            token = parse_folder_token(raw)
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        if token != cfg.folder_token:
            notes.append("文件夹 token：%s → %s" % (cfg.folder_token or "(空)", token))
            cfg.folder_token = token

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
        notes.append("⚠ 当前未配置 API Key，规则四/五（LLM 判定）不会启用")

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

    # 5) 各条规则的启停开关（每条规则独立可关）
    rules_payload = payload.get("rules_enabled")
    if isinstance(rules_payload, dict):
        for k in cfg.rules_enabled.keys():
            if k in rules_payload:
                v = bool(rules_payload[k])
                if v != cfg.rules_enabled[k]:
                    cfg.rules_enabled[k] = v
                    name = {"r1": "规则一·AI痕迹词", "r2": "规则二·违禁词",
                            "r3": "规则三·关键词位置", "r4": "规则四·观点级主角性",
                            "r5": "规则五·AI人味&收录友好度",
                            "r6": "规则六·广告法绝对化用语",
                            "r7": "规则七·FAQ问答结构",
                            "r8": "规则八·竞品联系方式",
                            "r9": "规则九·品牌负面描述"}.get(k, k)
                    notes.append("%s：%s" % (name, "启用" if v else "停用"))
        # 开启 LLM 类规则却未配 key 时给出提醒
        if (cfg.rules_enabled.get("r4") or cfg.rules_enabled.get("r5")
                or cfg.rules_enabled.get("r9")) and not cfg.llm.api_key:
            notes.append("⚠ 已开启 LLM 类规则但当前未配置 API Key，对应规则不会生效")

    # 6) 并发审核数（1-16；数据量大时调高可提速，过高可能触发 LLM 限流）
    conc = payload.get("concurrency")
    if conc not in (None, ""):
        try:
            n = int(conc)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "并发数必须是 1-16 的整数"}
        if not 1 <= n <= 16:
            return {"ok": False, "msg": "并发数必须在 1-16 之间"}
        if n != cfg.concurrency:
            notes.append("并发审核数：%d → %d（重启机器人后生效）" % (cfg.concurrency, n))
            cfg.concurrency = n

    try:
        save_config(cfg)
    except Exception as e:
        return {"ok": False, "msg": "保存 config.yaml 失败: %s" % e}
    if cred_changed:
        notes.append("⚠ 应用凭证变更需重启机器人后生效（下方「保存并重启」会自动处理）")
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

    # ---- Basic Auth ----
    def _check_auth(self):
        # 没配用户名密码 = 不强制（开发场景默认 127.0.0.1 仍安全）
        if not (PANEL_USER and PANEL_PASS):
            return True
        import base64
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            up = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
            u, _, p = up.partition(":")
            return u == PANEL_USER and p == PANEL_PASS
        except Exception:
            return False

    def _require_auth(self):
        if self._check_auth():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="FeishuAuditPanel"')
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"401 Unauthorized")
        return False

    # ---- 路由 ----
    def do_GET(self):
        if not self._require_auth():
            return
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
        if not self._require_auth():
            return
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
    else:
        # 开机自启场景：面板起来时自动拉起 bot（已配置 autostart_bot 才生效）
        if os.environ.get("FEISHU_AUDIT_AUTOSTART_BOT", "0") == "1":
            r = bot_start()
            print("[panel] 开机自启 bot: %s" % r, flush=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("[panel] 运维面板已启动:  http://%s:%d" % (HOST, PORT), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
