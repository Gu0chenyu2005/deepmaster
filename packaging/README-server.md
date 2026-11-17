# 服务端

Ubuntu，解压后部署：

```bash
mkdir -p ~/username
tar -xzf server-review-*.tar.gz -C ~/username
sudo bash ~/username/dm-web/install.sh
```

访问 `http://服务器IP:8790`。初始账号 `admin`，密码 `admin123`；首次部署可用 `sudo DM_ADMIN_PASSWORD=自定义密码 bash ~/username/dm-web/install.sh`。

服务自动启动并开机自启；重启：`sudo systemctl restart dm-web-api dm-web`。
