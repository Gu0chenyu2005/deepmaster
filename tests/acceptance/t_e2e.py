# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
import json
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8765/api"
ADMIN = ("admin", "admin123")
R = []


def chk(name, cond, extra=""):
    R.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (("  | " + str(extra)) if extra else ""))


def call(method, path, body=None, tok=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"raw": raw}


def main():
    _, d = call("POST", "/auth/login", {"account": ADMIN[0], "password": ADMIN[1]})
    tok = d["data"]["token"]


    _, d = call("GET", "/users", tok=tok)
    users = d["data"]["users"]
    chk("库里只剩 admin", [u["account"] for u in users] == ["admin"], users)
    _, d = call("GET", "/stats", tok=tok)
    s = d["data"]
    chk("采集时长已归零", s["capture"]["total_s"] == 0, s["capture"])
    chk("标注时长已归零", s["annotate"]["total_s"] == 0, s["annotate"])
    chk("审核记录已归零", s["review"]["counted"] == 0, s["review"])
    chk("数据集为空", s["dataset"]["datasets"] == 0, s["dataset"])
    _, d = call("GET", "/overview", tok=tok)
    ov = d["data"]
    chk("总览四环全 0",
        ov["quality"]["total"] == 0 and ov["improve"]["pending_total"] == 0
        and ov["errors"]["total"] == 0, ov)
    _, d = call("GET", "/dirs?path=&remote=0", tok=tok)
    chk("服务器本地 datasets 为空", d["data"].get("dirs") == [], d["data"])
    _, d = call("POST", "/agent/scan", {}, tok=tok)
    devs = d["data"]["devices"]
    ids = [x.get("device_id") for x in devs]
    chk("扫描到采集端", len(devs) >= 1, ids)
    _, d = call("POST", "/agent/connect",
                {"ip": "127.0.0.1", "discovery_port": 5111}, tok=tok)
    chk("连接采集端（服务器下发通信口）", d.get("ok"), d.get("data"))
    _, d = call("GET", "/system", tok=tok)
    sysd = d["data"]
    chk("Orin 系统状态转发（CPU/内存/磁盘）",
        bool(sysd.get("cpu")) and bool(sysd.get("memory")) and bool(sysd.get("disk")), 
        {"cpu": sysd.get("cpu", {}).get("percent"), "mem": sysd.get("memory", {}).get("percent")})
    _, d = call("GET", "/config", tok=tok)
    cfg = (d.get("data") or {}).get("config") or {}
    ag = cfg.get("_agent") or {}
    chk("Orin 程序配置读回（config + _agent 元信息）",
        bool(cfg.get("k7_dir")) and ag.get("reachable") is True,
        {"k7_dir": cfg.get("k7_dir"), "agent_url": ag.get("url"),
         "data_root": ag.get("data_root")})
    _, d = call("GET", "/run/status", tok=tok)
    chk("Orin 程序运行状态", d.get("ok"), d.get("data"))
    _, d = call("POST", "/run/preflight", {"cfg": {}}, tok=tok, timeout=60)
    pre = d.get("data") or {}
    chk("下发 preflight 指令到 Orin", d.get("ok") and pre.get("action") == "preflight",
        {k: pre.get(k) for k in ("ok", "error")})
    _, d = call("GET", "/cam/health", tok=tok)
    chk("相机状态转发（该端以 --no-cam 启动）", d.get("ok"), d.get("data"))
    _, d = call("GET", "/dirs?path=&remote=1", tok=tok)
    names = [x["name"] for x in d["data"].get("dirs", [])]
    chk("采集页能看到 Orin 上的目录", "e2e" in names, names)
    _, d = call("GET", "/config", tok=tok)
    cfg = (d.get("data") or {}).get("config") or {}
    agent_url = ((cfg.get("_agent") or {}).get("url") or "").rstrip("/")
    data_root = ((cfg.get("_agent") or {}).get("data_root")
                 or cfg.get("local_data_dir") or "")
    _, d = call("POST", "/sessions", {"dir": data_root, "folder": "e2e"}, tok=tok)
    sid = (d.get("data") or {}).get("id")
    chk("创建结束会话以触发 Orin 压缩", bool(sid), d.get("data"))
    if sid:
        _, ended = call("PATCH", "/sessions/" + sid,
                        {"duration_s": 0, "state": "done"}, tok=tok)
        chk("结束会话已启动压缩任务",
            (ended.get("data") or {}).get("archive", {}).get("state") == "compressing",
            ended.get("data"))
    archive_state = {}
    if agent_url:
        for _ in range(120):
            req = urllib.request.Request(agent_url + "/api/files/archive-status?dir=e2e")
            with urllib.request.urlopen(req, timeout=10) as r:
                archive_state = json.loads(r.read().decode()).get("data") or {}
            if archive_state.get("state") in ("ready", "error"):
                break
            time.sleep(1)
    chk("Orin 已生成压缩包", archive_state.get("state") == "ready", archive_state)

    _, d = call("POST", "/uploads", {"dirs": ["e2e"]}, tok=tok)
    chk("网页发起压缩包回传", d.get("ok"), d.get("data"))

    state = {}
    for _ in range(60):
        time.sleep(1.5)
        _, d = call("GET", "/uploads", tok=tok)
        state = d["data"] or {}
        if not state.get("running"):
            break
    chk("接收并解压完成", any(x.get("state") == "done" for x in (state.get("dirs") or [])),
        state.get("dirs"))

    import os
    local = "/home/edisonchen/Desktop/deepmaster/datasets/e2e"
    files = os.listdir(local) if os.path.isdir(local) else []
    chk("数据落到服务器 datasets/", any(f.endswith(".hdf5") for f in files), files)
    leftovers = [f for f in os.listdir(os.path.dirname(local)) if f.startswith(".dm-upload-")]
    chk("服务器解压后已清理压缩包临时文件", leftovers == [], leftovers)


    _, d = call("GET", "/dirs?path=&remote=0", tok=tok)
    names = [x["name"] for x in d["data"].get("dirs", [])]
    chk("审核页看到回传后的目录", "e2e" in names, names)
    _, d = call("GET", "/episodes?dir=e2e", tok=tok)
    eps = d["data"].get("episodes") or []
    chk("episode 解析成功", len(eps) == 1, eps[:2])
    if eps:
        _, d = call("GET", "/episode/0/meta?dir=e2e", tok=tok)
        m = d["data"]
        chk("episode meta 可读", d.get("ok") and m.get("frames", 0) > 0,
            {k: m.get(k) for k in ("frames", "fps", "duration_s")})
        _, d = call("GET", "/episode/0/curves?dir=e2e", tok=tok)
        chk("曲线数据可读", d.get("ok") and bool(d["data"]), type(d["data"]).__name__)

        req = urllib.request.Request(API + "/episode/0/frame/wrist/0?dir=e2e")
        req.add_header("Authorization", "Bearer " + tok)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(4)
            st = r.status
        chk("抽帧可读（JPEG）", st == 200 and raw[:2] == b"\xff\xd8", (st, raw))


        _, d = call("POST", "/review", {"dir": "e2e", "episode": 0,
                                        "status": "good", "comment": "e2e 核对"}, tok=tok)
        chk("保存审核结果", d.get("ok"), d.get("data"))
        _, d = call("GET", "/overview", tok=tok)
        chk("总览统计随审核更新", d["data"]["quality"]["good"] == 1,
            d["data"]["quality"])
    print("\n合计 %d 项，通过 %d 项" % (len(R), sum(1 for _, ok in R if ok)))
    return 0 if all(ok for _, ok in R) else 1


if __name__ == "__main__":
    raise SystemExit(main())
