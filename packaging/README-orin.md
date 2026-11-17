# 采集端

Ubuntu aarch64，需已安装 ROS Humble 和 RealSense 驱动：

```bash
sudo apt-get install -y zstd
sudo tar --zstd -xf orin-collect-*.tar.zst -C /opt
sudo bash /opt/dm-collect/install.sh
```

网页中连接设备，在“程序配置”填写 DDS 主机信息。相机自动识别，首次接线后核对三路视角；数据默认保存在 `/opt/dm-collect/data`。

服务开机自启；重启：`sudo systemctl restart dm-agent`。安装脚本仅启动代理，不启动机械臂采集。
