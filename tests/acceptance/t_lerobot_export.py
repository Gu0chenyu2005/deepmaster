# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""隔离环境中的浏览器导出验收，不访问在线设备。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from test_lerobot_export import api, exporter, episode, run_web


def main():
    with tempfile.TemporaryDirectory(prefix="dm-export-browser-") as temp:
        root = Path(temp)
        data = root / "datasets"
        source = data / "sample"
        source.mkdir(parents=True)
        episode(source / "episode_0_vla_valid.hdf5")
        episode(source / "episode_1_vla_valid.hdf5")
        backend = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), run_web.H)
        for server in (backend, proxy):
            server.daemon_threads = True
        env = {"DM_DB": str(root / "test.db"), "DM_SECRET": "browser-test-secret",
               "DM_ADMIN_PASSWORD": "browser-test", "DM_EXPORT_DIR": str(root / "exports")}
        with patch.dict(os.environ, env), patch.multiple(api, PROJECT_DIR=str(root), INIT_DATA=str(data),
                DEFAULT_DATA=str(data), ALLOW_ROOTS=[str(data)], _DATA_DIR_NAMES={"datasets"},
                _EXPORT_MANAGER=None), patch.object(run_web, "API_PORT", backend.server_port), \
                patch.object(run_web, "BACKEND", "http://127.0.0.1:%d" % backend.server_port):
            api.db.migrate()
            api.auth.ensure_bootstrap_user()
            with api.db.connect() as conn:
                conn.execute("INSERT INTO reviews(dir_path,episode,status,reviewed_at) VALUES(?,?,?,?)",
                             ("datasets/sample", 0, "good", api.db.now()))
            for server in (backend, proxy):
                threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors, qc_requests = [], []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.on("request", lambda r: qc_requests.append(r.url) if r.method == "POST" and "/autocheck" in r.url else None)
                    page.goto("http://127.0.0.1:%d" % proxy.server_port)
                    page.fill("#account", "admin")
                    page.fill("#password", "browser-test")
                    page.click("#btn-login")
                    page.wait_for_function("window.dmApi && window.dmApi.hasToken()")
                    page.wait_for_timeout(700)
                    page.click('.navitem[data-go="review"]')
                    export = page.locator("#rv-export")
                    expect(export).to_be_visible(timeout=15000)
                    expect(export).to_be_enabled()
                    qc_box = page.locator("#rv-autocheck").bounding_box()
                    export_box = export.bounding_box()
                    assert export_box["x"] > qc_box["x"]
                    export.click()
                    link = page.get_by_role("link", name="下载 LeRobot 数据集")
                    expect(link).to_be_visible(timeout=30000)
                    expect(page.locator("#rv-export-status")).to_contain_text("已导出 1 条合格数据")
                    with page.expect_download() as download_info:
                        link.click()
                    download_info.value.save_as(root / "download.tar.gz")
                    assert (root / "download.tar.gz").stat().st_size > 0
                    page.reload()
                    page.click('.navitem[data-go="review"]')
                    expect(page.get_by_role("link", name="下载 LeRobot 数据集")).to_be_visible(timeout=15000)
                    assert not qc_requests, qc_requests
                    assert not errors, errors
                    jobs = list((root / "exports").glob("*/job.json"))
                    assert len(jobs) == 1
                    job = json.loads(jobs[0].read_text())
                    assert job["total"] == 1 and job["state"] == "done"
                    browser.close()
                    print("PASS 独立按钮位置、仅合格导出、下载、刷新恢复及无质检调用")
            finally:
                for server in (proxy, backend):
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    main()
