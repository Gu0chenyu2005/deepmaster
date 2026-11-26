# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""本机采集程序执行器，供采集端代理及服务器本地模式调用。"""

import os
from functools import wraps
import fcntl
import pty
import re
import shutil
import shlex
import signal
import subprocess
import termios
import threading
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


K7_READY_PATTERN = os.environ.get("DM_K7_READY_PATTERN", r"\b200\b")
K7_READY_TIMEOUT = float(os.environ.get("DM_K7_READY_TIMEOUT", "30"))
RL_GRACE = float(os.environ.get("DM_RL_GRACE", "5"))
_lock = threading.Lock()
_procs = {}
_operation_lock = threading.RLock()


def serialized_operation(func):
    """拒绝并发控制操作，同线程嵌套调用共用锁。"""
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not _operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "设备正在执行其他控制操作，请等待完成"}
        try:
            return func(*args, **kwargs)
        finally:
            _operation_lock.release()
    return wrapped



K7_KEY = "k7master3"
_DDS_STATUS_CACHE = {}
RL_KEY = "rl_deploy"
SVC_KEY = "robotservice"
def _logfile(key):
    """打开进程日志，处理旧日志的写入权限。"""
    path = os.path.join(LOG_DIR, "%s.log" % key)
    try:
        return open(path, "ab", buffering=0)
    except PermissionError:

        try:
            if not _is_root():
                subprocess.run(
                    ["sudo", "-n", "chown", "%d:%d" % (os.getuid(), os.getgid()), path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
            return open(path, "ab", buffering=0)
        except Exception:
            alt = os.path.join(LOG_DIR, "%s.%d.log" % (key, os.getuid()))
            return open(alt, "ab", buffering=0)


def _is_root():
    """当前进程是否已经是 root。"""
    try:
        return os.geteuid() == 0
    except Exception:
        return False


def _sudo(pw):
    """按当前权限及配置构造 sudo 前缀，非交互模式使用 sudo -n。"""
    if _is_root():
        return ""
    if pw:
        return "echo %s | sudo -S -p '' " % shlex.quote(pw)
    return "sudo -n "


def _data_root(cfg):
    """优先使用 local_data_dir，兼容旧字段 capture_dir。"""
    for key in ("local_data_dir", "capture_dir"):
        v = _clean(cfg.get(key))
        if v:
            return _expand(v) if key == "capture_dir" else v
    return "datasets"


def _rl_deploy_work(cfg):
    """采集程序始终从标准 Home 构建目录启动。"""
    deploy_dir = _expand(_clean(cfg.get("rl_deploy_dir")))
    return os.path.join(deploy_dir, "build") if deploy_dir else ""

def _clean(v, default=""):
    if v is None:
        return default
    return str(v)


def _expand(path):
    """展开 ~ 与环境变量，得到真实路径（用于 cwd 校验）。"""
    if not path:
        return path
    return os.path.expanduser(os.path.expandvars(path))


@serialized_operation
def spawn(key, name, cmd, cwd=None, want_stdin=False, env=None):
    """替换同名进程；want_stdin 指定是否保留交互输入管道。"""
    stopped = stop(key)
    if not stopped.get("ok"):
        return stopped

    log_offset = _log_size(key)

    if cwd:
        real_cwd = _expand(cwd)
        if not os.path.isdir(real_cwd):
            return {"ok": False, "error": "目录不存在：%s" % real_cwd}
        cwd = real_cwd

    log = None
    master_fd = None
    slave_fd = None
    input_fd = None
    proc = None
    try:
        log = _logfile(key)
        is_shell = isinstance(cmd, str)
        if want_stdin:
            master_fd, slave_fd = pty.openpty()

            def _attach_tty():
                os.setsid()
                try:
                    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                except Exception:
                    pass

            stdin = stdout = slave_fd
        else:
            stdin = subprocess.DEVNULL
            stdout = log
        proc = subprocess.Popen(
            cmd, shell=is_shell, cwd=cwd,
            env=({**os.environ, **env} if env is not None else None),
            stdin=stdin, stdout=stdout,
            stderr=(slave_fd if want_stdin else subprocess.STDOUT),
            start_new_session=not want_stdin,
            preexec_fn=(_attach_tty if want_stdin else None),
            **({"executable": "/bin/bash"} if is_shell else {}),
        )
        if slave_fd is not None:
            os.close(slave_fd)
            slave_fd = None
            input_fd = os.dup(master_fd)
            threading.Thread(target=_read_pty, args=(master_fd, log),
                             name="pty-%s" % key, daemon=True).start()
        elif log is not None:
            try:
                log.close()
            except OSError:
                pass
            log = None
    except Exception as e:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        if log is not None:
            try:
                log.close()
            except OSError:
                pass
        for fd in (master_fd, slave_fd, input_fd):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
        return {"ok": False, "error": "启动失败：%s" % e}

    with _lock:
        _procs[key] = {"proc": proc, "started": time.time(), "cmd": cmd,
                       "name": name, "log_offset": log_offset,
                       "input_fd": input_fd}


    time.sleep(1.2)
    code = proc.poll()
    if code is not None:
        tail = _tail(key, 12)
        stop(key)
        return {"ok": False, "error": "进程立即退出（code %s）%s" % (code, tail)}
    return {"ok": True, "pid": proc.pid}


def _read_pty(fd, log):
    """把交互程序伪终端输出写入日志，避免管道缓冲阻塞。"""
    try:
        while True:
            data = os.read(fd, 16384)
            if not data:
                break
            log.write(data)
    except (OSError, ValueError):
        pass
    finally:
        try: os.close(fd)
        except OSError: pass
        try: log.close()
        except Exception: pass


def _log_size(key):
    try:
        return os.path.getsize(os.path.join(LOG_DIR, "%s.log" % key))
    except OSError:
        return 0


def _read_since(key, offset):
    try:
        with open(os.path.join(LOG_DIR, "%s.log" % key), "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _alive(key):
    with _lock:
        ent = _procs.get(key)
    if not ent:
        return False
    return ent["proc"].poll() is None


def _managed_control_only():
    with _lock:
        ent = _procs.get(RL_KEY)
    return bool(ent and ent["proc"].poll() is None and
                "ARMDOG_CONTROL_ONLY=1" in str(ent.get("cmd", "")))


def wait_for_pattern(key, pattern, offset=0, timeout=30.0, settle=0.6):
    """日志匹配后确认进程持续存活，返回就绪状态与等待时长。"""
    t0 = time.time()
    rx = re.compile(pattern)
    while time.time() - t0 < timeout:
        if not _alive(key):
            return {"ok": False, "error": "进程已退出", "tail": _tail(key)}
        if rx.search(_read_since(key, offset)):
            time.sleep(settle)
            if not _alive(key):
                return {"ok": False, "error": "进程在就绪后退出", "tail": _tail(key)}
            return {"ok": True, "waited_s": round(time.time() - t0, 1)}
        time.sleep(0.2)
    return {"ok": False, "error": "等待超时（%.0fs）" % timeout, "tail": _tail(key)}


def _tail(key, n=15):
    path = os.path.join(LOG_DIR, "%s.log" % key)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4000))
            lines = f.read().decode("utf-8", "replace").strip().splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def send_key(key, ch):
    """向交互式进程的 stdin 写入一个按键（不换行，与 pexpect send 一致）。"""
    with _lock:
        ent = _procs.get(key)
    if not ent:
        return {"ok": False, "error": "该进程不是由本服务启动的，无法写入 stdin"}
    proc = ent["proc"]
    if proc.poll() is not None:
        return {"ok": False, "error": "进程已退出"}
    try:
        input_fd = ent.get("input_fd")
        if input_fd is None:
            return {"ok": False, "error": "stdin 不可写"}
        os.write(input_fd, ch.encode())
        return {"ok": True, "sent": ch}
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s" % e}


@serialized_operation
def stop(key):
    """按进程组终止，先 TERM 后 KILL。"""
    with _lock:
        ent = _procs.get(key)
    if not ent:
        return {"ok": True, "already": True}
    proc = ent["proc"]
    if proc.poll() is not None:
        _close_process_input(ent)
        with _lock:
            _procs.pop(key, None)
        return {"ok": True, "already": True}
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    for _ in range(20):
        if proc.poll() is not None:
            break
        time.sleep(0.15)
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        time.sleep(0.2)
    if proc.poll() is None:
        return {"ok": False, "error": "进程未退出，保留管理状态以便重试"}
    _close_process_input(ent)
    with _lock:
        _procs.pop(key, None)
    return {"ok": True}


def _close_process_input(ent):
    fd = ent.get("input_fd")
    if fd is not None:
        ent["input_fd"] = None
        try:
            os.close(fd)
        except OSError:
            pass


def iter_procs():
    """遍历 /proc，产出 (pid, argv)。"""
    try:
        names = os.listdir("/proc")
    except Exception:
        return
    for entry in names:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                raw = f.read()
        except Exception:
            continue
        args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if args:
            yield pid, args


def find_pids(names):
    """仅匹配 argv 前两项的文件名，避免误识别无关命令。"""
    targets = set(n for n in (names or []) if n)
    if not targets:
        return []
    me = os.getpid()
    try:
        my_pgid = os.getpgid(me)
    except Exception:
        my_pgid = None

    out = []
    for pid, args in iter_procs():
        if pid == me:
            continue
        try:
            if my_pgid is not None and os.getpgid(pid) == my_pgid:
                continue
        except Exception:
            continue
        for a in args[:2]:
            if os.path.basename(a) in targets:
                out.append(pid)
                break
    return out


def pgrep_count(names):

    if isinstance(names, str):
        names = [names]
    return len(find_pids(names))


def status(patterns=None, cfg=None):

    patterns = patterns or {}
    out = {}
    with _lock:
        for key, ent in list(_procs.items()):
            code = ent["proc"].poll()
            if code is not None:
                _procs.pop(key, None)
                _close_process_input(ent)
                out[key] = {"running": False, "exit": code, "name": ent["name"]}
                continue
            out[key] = {
                "running": True,
                "pid": ent["proc"].pid,
                "uptime_s": int(time.time() - ent["started"]),
                "name": ent["name"],
            }


    for key, pat in patterns.items():
        if key in out and out[key].get("running"):
            continue
        if pgrep_count(pat) > 0:
            out[key] = {"running": True, "pid": None, "uptime_s": None,
                        "name": key, "external": True}
    if cfg and all(_clean(cfg.get(k)).strip() for k in ("dds_ip", "dds_username", "dds_password")):
        host = _clean(cfg.get("dds_ip"))
        cached = _DDS_STATUS_CACHE.get(host)
        if not cached or time.time() - cached["time"] > 5:
            result = _dds_run(cfg, "pgrep -x %s >/dev/null && echo running || echo stopped" %
                              shlex.quote(_clean(cfg.get("k7_bin"), "K7Master3")), timeout=8)
            cached = {"time": time.time(), "result": result}
            _DDS_STATUS_CACHE[host] = cached
        result = cached["result"]
        out[K7_KEY] = {"running": result["code"] == 0 and result["out"].strip() == "running",
                       "name": _clean(cfg.get("k7_bin"), "K7Master3"), "remote": True,
                       "host": host, "reachable": result["code"] == 0}
    return out


def _run_once(cmd, timeout=30):

    try:
        p = subprocess.run(cmd, shell=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return {"code": p.returncode,
                "out": (p.stdout or b"").decode("utf-8", "replace").strip()}
    except subprocess.TimeoutExpired:
        return {"code": -1, "out": "超时"}
    except Exception as e:
        return {"code": -1, "out": str(e)}


def _dry(cfg):
    """试运行采集流程，不启动实际程序。"""
    return str(cfg.get("dry_run") or "").lower() in ("1", "true", "yes", "on")


def _dds_run(cfg, script, timeout=30):
    host = _clean(cfg.get("dds_ip")).strip()
    user = _clean(cfg.get("dds_username")).strip()
    password = _clean(cfg.get("dds_password"))
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", host) or host.startswith("-") or not user or not password:
        return {"code": 2, "out": "请配置有效的 DDS IP、用户名和密码"}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", user) or user.startswith("-"):
        return {"code": 2, "out": "DDS 用户名格式无效"}
    if not shutil.which("sshpass"):
        return {"code": 2, "out": "Orin 缺少 sshpass，请安装 openssh-client 和 sshpass"}
    env = dict(os.environ, SSHPASS=password)
    remote = shlex.join(["sudo", "-S", "-p", "", "--", "bash", "-c", script])
    command = ["sshpass", "-e", "ssh", "-T", "-o", "ConnectTimeout=5",
               "-o", "StrictHostKeyChecking=accept-new", "--", "%s@%s" % (user, host),
               remote]
    try:
        p = subprocess.run(command, input=password + "\n", text=True, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return {"code": p.returncode, "out": (p.stdout or "").strip()}
    except subprocess.TimeoutExpired:
        return {"code": -1, "out": "DDS SSH 请求超时"}
    except Exception as e:
        return {"code": -1, "out": str(e)}


@serialized_operation
def run_t1(cfg):
    """按需启动 K7Master3，并等待返回值 236 稳定五秒。"""
    if _dry(cfg):
        return {"ok": True, "dry_run": True, "pid": None}
    if not all(_clean(cfg.get(k)).strip() for k in ("dds_ip", "dds_username", "dds_password")):
        return {"ok": False, "error": "请先配置 DDS 主机 IP、用户名和密码"}
    directory = _clean(cfg.get("k7_dir"), "/home/user")
    library = _clean(cfg.get("k7_lib_path"), directory)
    binary = _clean(cfg.get("k7_bin"), "K7Master3")
    chrt = _clean(cfg.get("k7_chrt")).strip()
    taskset = _clean(cfg.get("k7_taskset")).strip()
    taskset_cmd = "taskset -c %s " % shlex.quote(taskset) if taskset else ""
    launch = "./%s" % shlex.quote(binary)
    script = "\n".join([
        "set -eu",
        "cd %s" % shlex.quote(directory),
        "export LD_LIBRARY_PATH=%s${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" % shlex.quote(library),
        "log=/var/log/deepmaster/k7master3.log",
        "pid=$(pgrep -xo %s || true)" % shlex.quote(binary),
        "if [ -n \"$pid\" ]; then",
        "  echo 'K7Master3 already running; reusing it'",
        "  out=$(readlink -f /proc/\"$pid\"/fd/1 2>/dev/null || true)",
        "  if [ -n \"$out\" ] && [ -f \"$out\" ] && [ -r \"$out\" ]; then log=$out; fi",
        "  owner=$(ps -o euid= -p \"$pid\" | tr -d ' ')",
        "  if [ \"$owner\" != 0 ] && grep -Eq 'Operation not permitted|no operate permitted' \"$log\" 2>/dev/null; then",
        "    echo 'K7Master3 is already running without required hardware privileges; stop the stale process before retrying'",
        "    exit 1",
        "  fi",
        "else",
        "  install -d -m 0700 /var/log/deepmaster",
        "  : > \"$log\"",
        "launch_k7() { nohup \"$@\" > \"$log\" 2>&1 < /dev/null & pid=$!; }",
        ("launch_k7 chrt %s %s%s" % (shlex.quote(chrt), taskset_cmd, launch)) if chrt
        else ("launch_k7 %s%s" % (taskset_cmd, launch)),
        "  sleep 1",
        "  if [ -n %s ] && ! kill -0 \"$pid\" 2>/dev/null && grep -q 'chrt: failed to set pid' \"$log\"; then" % shlex.quote(chrt),
        "    echo 'chrt unavailable; retrying without realtime priority'",
        "    launch_k7 %s%s" % (taskset_cmd, launch),
        "  fi",
        "fi",
        "[ -r \"$log\" ] || { echo 'K7 stdout is not readable; cannot verify return value 236'; exit 1; }",
        "deadline=$((SECONDS + 60))",
        "stable_since=",
        "while [ \"$SECONDS\" -lt \"$deadline\" ]; do",
        "  line=$(tail -n 1 \"$log\" 2>/dev/null || true)",
        "  if printf '%s' \"$line\" | grep -Eq '(^|[^0-9])236([^0-9]|$)'; then",
        "    [ -n \"$stable_since\" ] || stable_since=$SECONDS",
        "    [ -n \"$stable_since\" ] && [ $((SECONDS - stable_since)) -ge 5 ] && { echo 'ready: 236 stable for 5 seconds'; exit 0; }",
        "  else",
        "    stable_since=",
        "  fi",
        "  kill -0 \"$pid\" 2>/dev/null || { tail -n 40 \"$log\"; exit 1; }",
        "  sleep 0.1",
        "done",
        "echo 'K7 did not report return value 236 stable for 5 seconds within 60 seconds'",
        "tail -n 40 \"$log\"",
        "exit 1",
    ])
    result = _dds_run(cfg, script, timeout=75)
    if result["code"]:
        return {"ok": False, "error": "DDS 上启动 K7Master3 失败：%s" % result["out"][-2000:]}
    return {"ok": True, "remote": True, "host": _clean(cfg.get("dds_ip")),
            "already_running": "already running" in result["out"], "waited_s": 5}

_SETHOME_CONTROL = r'''
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

def group_alive(pgid):
    for entry in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = entry.read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in ('Z', 'X'):
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False

def stop_group(proc):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            proc.poll()
            if not group_alive(proc.pid):
                proc.wait(timeout=1)
                return
            time.sleep(0.05)
    raise RuntimeError('SetHome process group could not be stopped')

def interrupted(signum, frame):
    raise RuntimeError('SetHome controller interrupted: %s' % signum)

for sig in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, interrupted)

proc = None
error = None
with tempfile.TemporaryFile() as output:
    try:
        proc = subprocess.Popen([sys.argv[1], '-1'], stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise RuntimeError('SetHome exited before five seconds (code %s)' % code)
    except Exception as exc:
        error = str(exc)
    finally:
        if proc is not None:
            try:
                stop_group(proc)
            except Exception as exc:
                error = str(exc)
        output.seek(0, 2)
        output.seek(max(0, output.tell() - 4096))
        if error:
            print(output.read().decode('utf-8', 'replace'))
            print(error)
            sys.exit(1)
print('deepmaster-sethome: completed five-second run; process group stopped')
'''


@serialized_operation
def run_tare(cfg):
    """执行五秒标零，并回收本次启动的远端进程组。"""

    if _dry(cfg):
        return {"ok": True, "dry_run": True}
    bin_name = _clean(cfg.get("rl_deploy_bin"), "rl_deploy")
    if _alive(RL_KEY) or pgrep_count([bin_name]) > 0:
        return {"ok": False, "error": "控制程序运行中，不能标零"}
    if not all(_clean(cfg.get(k)).strip() for k in ("dds_ip", "dds_username", "dds_password")):
        return {"ok": False, "error": "请先配置 DDS 主机 IP、用户名和密码"}
    k7_dir = _clean(cfg.get("k7_dir"), "/home/user")
    k7_lib = _clean(cfg.get("k7_lib_path"), k7_dir)
    sethome = _clean(cfg.get("k7_sethome_bin"), "SetHome")
    script = "\n".join([
        "set -eu",
        "cd %s" % shlex.quote(k7_dir),
        "export LD_LIBRARY_PATH=%s${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" % shlex.quote(k7_lib),
        "python3 -c %s %s" % (shlex.quote(_SETHOME_CONTROL), shlex.quote("./" + sethome)),
    ])
    result = _dds_run(cfg, script, timeout=15)
    if result["code"] != 0 or "deepmaster-sethome: completed five-second run; process group stopped" not in result["out"]:
        return {"ok": False, "error": "DDS 上 SetHome 执行失败：%s" % result["out"][-2000:]}
    return {"ok": True, "remote": True, "host": _clean(cfg.get("dds_ip"))}

@serialized_operation
def run_t2(cfg, control_only=False):
    """启动 手柄服务 + rl_deploy。"""
    if _dry(cfg):
        return {"ok": True, "dry_run": True, "pid": None}
    bin_name = _clean(cfg.get("rl_deploy_bin"), "rl_deploy")
    if _alive(RL_KEY):
        if control_only and _managed_control_only():
            return {"ok": True, "already": True, "control_only": True}
        if _managed_control_only():
            return {"ok": False,
                    "error": "远控已退出，控制程序仍在阻尼保持机械臂；请在机械臂得到支撑并确认安全后处理该进程"}
        return {"ok": False, "error": "采集程序已经运行，请先结束当前采集"}
    if pgrep_count([bin_name]):
        return {"ok": False, "error": "检测到其他位置的采集程序正在运行，请先停止该程序"}
    if control_only:
        scheduling = _validate_control_scheduling()
        if not scheduling["ok"]:
            return scheduling
    pw = _sudo(cfg.get("sudo_pw"))

    svc_dir = _clean(cfg.get("robot_service_dir"))
    svc_script = _clean(cfg.get("robot_service_script"))
    deploy_dir = _clean(cfg.get("rl_deploy_dir"))
    if control_only:
        package_root = os.path.realpath(PROJECT_DIR)
        for label, path in (("手柄服务", svc_dir), ("rl_deploy", deploy_dir)):
            real = os.path.realpath(_expand(path)) if path else ""
            if not real or (real != package_root and not real.startswith(package_root + os.sep)):
                return {"ok": False, "error": "远控仅控制的%s必须来自采集端部署包" % label}

    service_running = _service_process_exists(svc_script) if svc_dir and svc_script else False
    if control_only and service_running:
        scheduling = _pin_control_service(svc_dir)
        if not scheduling["ok"]:
            return scheduling
    if svc_dir and svc_script and not service_running:
        priority = "taskset -c 8-11 nice -n -10 " if control_only else ""
        svc_cmd = "cd %s && %s%s./%s" % (
            shlex.quote(svc_dir), pw, priority, shlex.quote(svc_script))
        r = spawn(SVC_KEY, "手柄服务", svc_cmd, cwd=svc_dir)
        if not r.get("ok"):
            return r


    ros = _clean(cfg.get("ros_setup"))
    data_root = _data_root(cfg)
    fps = _clean(cfg.get("record_fps"))
    imu = _clean(cfg.get("imu_topic"))

    work = _rl_deploy_work(cfg)
    parts = []
    if ros:
        parts.append("[ -f %s ] && . %s" % (shlex.quote(_expand(ros)), shlex.quote(_expand(ros))))
    if data_root:
        parts.append("export ARMDOG_DATA_ROOT=%s" % shlex.quote(data_root))
    if fps:
        parts.append("export ARMDOG_RECORD_FPS=%s" % shlex.quote(fps))
    if imu:
        parts.append("export ARMDOG_IMU_TOPIC=%s" % shlex.quote(imu))
    if control_only:
        parts.extend([
            "export ARMDOG_CONTROL_ONLY=1",
            "export ARMDOG_CAMERA_DISABLE=1",
            "export ARMDOG_SHARED_FRAME_ENABLE=0",
            "exec taskset -c 8-11 nice -n -10 ./%s" % shlex.quote(bin_name),
        ])
    else:
        parts.append("exec ./%s" % shlex.quote(bin_name))

    cmd = "; ".join(parts)
    return spawn(RL_KEY, "rl_deploy", cmd, cwd=work, want_stdin=True)


def _validate_control_scheduling():
    """Verify the privileged agent can apply the fixed control CPU set and nice level."""
    if not hasattr(os, "sched_getaffinity"):
        return {"ok": False, "error": "当前系统不支持 CPU 亲和性调度"}
    try:
        available = set(os.sched_getaffinity(0))
    except OSError as exc:
        return {"ok": False, "error": "读取可用 CPU 失败：%s" % exc}
    required = set(range(4, 12))
    if not required.issubset(available):
        return {"ok": False, "error": "远控仅控制调度需要 CPU 4–11，可用 CPU：%s" %
                ",".join(map(str, sorted(available)))}
    if os.geteuid() != 0:
        return {"ok": False, "error": "远控仅控制调度需要采集端代理以 root 权限运行"}
    return {"ok": True}


def _service_process_exists(script="runService.sh"):
    """检测已由 systemd 或旧代理启动的手柄服务，避免重复占用 SDK 端口。"""
    names = {os.path.basename(script), "RoboticsServiceProcess"}
    return bool(find_pids(names))


def _pin_control_service(service_dir):
    """只调整部署包内已运行的手柄程序及其线程。"""
    root = os.path.realpath(_expand(service_dir))
    pids = find_pids(["RoboticsServiceProcess"])
    managed = []
    for pid in pids:
        try:
            executable = os.path.realpath("/proc/%d/exe" % pid)
            if os.path.commonpath((root, executable)) != root:
                return {"ok": False,
                        "error": "检测到部署包外运行的 RoboticsServiceProcess，未修改其调度；请先通过原启动端停止它"}
            managed.append(pid)
        except (OSError, ValueError):
            continue
    if not managed:
        return {"ok": False, "error": "手柄服务已存在但无法确认其来自采集端部署包"}

    cpus = set(range(8, 12))
    try:
        for pid in managed:
            task_dir = "/proc/%d/task" % pid
            tids = [int(tid) for tid in os.listdir(task_dir)]
            if not tids:
                tids = [pid]
            for tid in tids:
                try:
                    os.sched_setaffinity(tid, cpus)
                    os.setpriority(os.PRIO_PROCESS, tid, -10)
                    if os.sched_getaffinity(tid) != cpus or os.getpriority(os.PRIO_PROCESS, tid) != -10:
                        raise OSError("调度参数校验失败（TID %d）" % tid)
                except ProcessLookupError:
                    if tid == pid:
                        raise
                    continue
    except (OSError, ProcessLookupError) as exc:
        return {"ok": False, "error": "无法设置已运行手柄服务的 CPU 亲和性或 nice -10：%s" % exc}
    return {"ok": True}

def kill_by_name(name, grace=1.5):
    """按文件名匹配进程并终止其进程组。"""
    name = (name or "").strip()
    if not name:
        return 0

    pids = find_pids([name])
    if not pids:
        return 0

    me = os.getpid()
    try:
        my_pgid = os.getpgid(me)
    except Exception:
        my_pgid = None

    groups = set()
    for pid in pids:
        try:
            pgid = os.getpgid(pid)
        except Exception:
            continue
        if my_pgid is not None and pgid == my_pgid:
            continue
        groups.add(pgid)

    killed = 0
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGTERM)
            killed += 1
        except Exception:
            pass

    if killed:
        time.sleep(grace)
        for pgid in groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
    return killed


@serialized_operation
def inject_z(cfg):
    """向 rl_deploy 发送 z，启动机械臂。"""
    if _dry(cfg):
        return {"ok": True, "dry_run": True}
    return send_key(RL_KEY, "z")


@serialized_operation
def start_service(cfg):
    """单独启动手柄服务（runService）。"""
    if _dry(cfg):
        return {"ok": True, "dry_run": True, "pid": None}
    pw = _sudo(cfg.get("sudo_pw"))
    svc_dir = _clean(cfg.get("robot_service_dir"))
    svc_script = _clean(cfg.get("robot_service_script"), "runService.sh")

    if not svc_dir:
        return {"ok": False, "error": "手柄服务目录未配置"}
    if not os.path.isdir(_expand(svc_dir)):
        return {"ok": False, "error": "手柄服务目录不存在：%s" % _expand(svc_dir)}

    if _service_process_exists(svc_script):
        return {"ok": True, "already": True, "name": "手柄服务"}
    cmd = "cd %s && %s./%s" % (shlex.quote(svc_dir), pw, shlex.quote(svc_script))
    return spawn(SVC_KEY, "runService", cmd, cwd=svc_dir)

@serialized_operation
def stop_service(cfg):
    """停止手柄服务。"""
    if _dry(cfg):
        return {"ok": True, "dry_run": True}
    return stop(SVC_KEY)


@serialized_operation
def start_deploy(cfg):
    """单独启动 rl_deploy（注入环境变量，保留 stdin 用于后续 z / d）。"""
    if _dry(cfg):
        return {"ok": True, "dry_run": True, "pid": None}

    deploy_dir = _clean(cfg.get("rl_deploy_dir"))
    bin_name = _clean(cfg.get("rl_deploy_bin"), "rl_deploy")
    if _alive(RL_KEY):
        return {"ok": False, "error": "采集程序已经运行，请先结束当前采集"}
    if pgrep_count([bin_name]):
        return {"ok": False, "error": "检测到其他位置的采集程序正在运行，请先停止该程序"}
    ros = _clean(cfg.get("ros_setup"))
    data_root = _data_root(cfg)
    fps = _clean(cfg.get("record_fps"))
    imu = _clean(cfg.get("imu_topic"))

    if not deploy_dir:
        return {"ok": False, "error": "采集程序目录未配置"}

    work = _rl_deploy_work(cfg)
    if not os.path.isdir(work):
        return {"ok": False, "error": "采集程序目录不存在：%s" % work}

    parts = []
    if ros:
        parts.append("[ -f %s ] && . %s" % (shlex.quote(_expand(ros)), shlex.quote(_expand(ros))))
    if data_root:
        parts.append("export ARMDOG_DATA_ROOT=%s" % shlex.quote(data_root))
    if fps:
        parts.append("export ARMDOG_RECORD_FPS=%s" % shlex.quote(fps))
    if imu:
        parts.append("export ARMDOG_IMU_TOPIC=%s" % shlex.quote(imu))
    parts.append("exec ./%s" % shlex.quote(bin_name))

    cmd = "; ".join(parts)
    return spawn(RL_KEY, "rl_deploy", cmd, cwd=work, want_stdin=True)

@serialized_operation
def stop_deploy(cfg):
    """停止 rl_deploy（不经 stdio 握手，直接按进程组清理）。"""
    if _dry(cfg):
        return {"ok": True, "dry_run": True}
    return stop(RL_KEY)


@serialized_operation
def stop_capture(cfg=None):
    """结束当前录制并停止采集进程，保留 K7 与手柄服务的保持状态。"""
    cfg = cfg or {}
    if _dry(cfg):
        return {"ok": True, "dry_run": True, "steps": []}

    sent = send_key(RL_KEY, "d")
    if not sent.get("ok"):
        return {"ok": False, "error": sent.get("error", "停止采集指令发送失败"),
                "steps": [{"step": "send_d", **sent}]}

    time.sleep(RL_GRACE)
    stopped = stop(RL_KEY)
    if not stopped.get("ok"):
        return {"ok": False, "error": stopped.get("error", "采集进程停止失败"),
                "steps": [{"step": "send_d", "ok": True},
                          {"step": "stop_rl_deploy", **stopped}]}
    return {"ok": True, "steps": [{"step": "send_d", "ok": True},
                                    {"step": "wait", "seconds": RL_GRACE},
                                    {"step": "stop_rl_deploy", **stopped},
                                    {"step": "keep_k7master3", "ok": True},
                                    {"step": "keep_robotservice", "ok": True}]}


@serialized_operation
def stop_control_only(cfg=None):
    """按近控停止顺序退出仅控制模式。"""
    cfg = cfg or {}
    if not _alive(RL_KEY):
        if pgrep_count([_clean(cfg.get("rl_deploy_bin"), "rl_deploy")]):
            return {"ok": False, "error": "采集程序由其他进程或旧代理管理，无法确认已退出，请通过原启动端处理"}
        return {"ok": True, "already": True, "steps": []}
    return stop_capture(cfg)


@serialized_operation
def stop_k7_force(cfg):
    if _dry(cfg):
        return {"ok": True, "dry_run": True}
    binary = _clean(cfg.get("k7_bin"), "K7Master3")
    script = "pkill -KILL -x %s 2>/dev/null || true; systemctl stop cm1-k7master3.service 2>/dev/null || true" % shlex.quote(binary)
    result = _dds_run(cfg, script, timeout=12)
    if result["code"]:
        return {"ok": False, "error": "DDS 上停止 K7Master3 失败：%s" % result["out"][-2000:]}
    return {"ok": True, "remote": True, "force": True}


@serialized_operation
def stop_all(cfg=None):
    """依次发送 d、等待落盘、停止 rl_deploy、K7Master3 及手柄服务。"""
    cfg = cfg or {}
    if _dry(cfg):
        return {"dry_run": True, "skipped": True, "steps": []}

    steps = []


    rl_running_before = _alive(RL_KEY)
    sent = send_key(RL_KEY, "d")
    steps.append({"step": "send_d", "ok": bool(sent.get("ok")), **{
        k: v for k, v in sent.items() if k != "ok"}})

    if rl_running_before or sent.get("ok"):
        time.sleep(RL_GRACE)
        steps.append({"step": "wait", "seconds": RL_GRACE})
    else:
        steps.append({"step": "wait", "seconds": 0,
                      "note": "rl_deploy 未运行，跳过等待"})


    result = stop(RL_KEY)
    steps.append({"step": "stop_rl_deploy", **result})


    k7_result = stop_k7_force(cfg)
    steps.append({"step": "stop_k7master3", "ok": bool(k7_result.get("ok")),
                  "remote": True, "error": k7_result.get("error")})


    result = stop(SVC_KEY)
    steps.append({"step": "stop_robotservice", **result})

    return {"ok": True, "steps": steps, "grace_s": RL_GRACE}

def preflight_local(cfg):

    problems = []
    checks = {}

    for label, key, need_dir in (
        ("rl_deploy 目录", "rl_deploy_dir", True),
        ("手柄服务目录", "robot_service_dir", True),
    ):
        raw = _clean(cfg.get(key))
        if not raw:
            problems.append("%s 未配置" % label)
            continue
        p = _expand(raw)
        if need_dir and not os.path.isdir(p):
            problems.append("%s 不存在：%s" % (label, p))
        else:
            checks[key] = p


    if _dry(cfg):
        checks["sudo"] = None
        checks["dry_run"] = True
        return {"ok": not problems, "problems": problems, "checks": checks}


    pw = _sudo(cfg.get("sudo_pw"))
    r = _run_once("%strue" % pw, timeout=8)
    checks["sudo"] = (r["code"] == 0)
    if r["code"] != 0:
        problems.append("sudo 不可用（后端需以 root 运行，或在配置里填 sudo 密码）")
    return {"ok": not problems, "problems": problems, "checks": checks}
