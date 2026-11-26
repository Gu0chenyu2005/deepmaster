# 服务端

Ubuntu 22.04 amd64，解压后离线部署。依赖已包含在部署包中，清单见 `third_party/dependencies.json`：

```bash
mkdir -p ~/username
tar -xzf server-review-*.tar.gz -C ~/username
sudo bash ~/username/dm-web/install.sh
```

访问 `http://服务器IP:8790`。初始账号 `admin`，密码 `admin123`；首次部署可用 `sudo DM_ADMIN_PASSWORD=自定义密码 bash ~/username/dm-web/install.sh`。

服务自动启动并开机自启；重启：`sudo systemctl restart dm-web-api dm-web`。

数据审核页面点击“导出 LeRobot”，仅导出合格数据；结果保存在部署目录的 `exports/`，完成后可下载。
