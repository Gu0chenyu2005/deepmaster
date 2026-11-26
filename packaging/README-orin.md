# 采集端

Jetson Orin aarch64，需预装 NVIDIA JetPack、ROS 2 Humble 和 librealsense 驱动。部署包内含 Ubuntu Jammy arm64 的离线系统依赖；安装时无需联网。远控仅控制还需要与系统驱动匹配的 `pyrealsense2`。依赖清单见 `third_party/dependencies.json`。

```bash
sudo tar -xzf orin-collect-*.tar.gz -C /opt
sudo bash /opt/dm-collect/install.sh
```

网页中连接设备，在“程序配置”填写 DDS 主机信息。相机自动识别，首次接线后核对三路视角；数据默认保存在 `/opt/dm-collect/data`。

服务开机自启；重启：`sudo systemctl restart dm-agent`。安装脚本仅启动代理，不启动机械臂采集。
