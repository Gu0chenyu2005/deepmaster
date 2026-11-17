<img width="1344" height="360" alt="deepmaster-logo" src="https://github.com/user-attachments/assets/1b172453-8831-46bb-859f-6acd114caee8" />
足臂机器人数据采集与审核平台，采用服务端与采集端分离架构，支持设备管理、三相机实时预览、遥操作采集及数据审核。

- `server/`：服务端与采集端代理代码
- `assets/`、`index.html`：网页界面
- `orin/`：采集端相机与数据路径适配
- `packaging/`：部署脚本与说明
- `tests/`：测试

服务端使用 Ubuntu；采集端使用 Ubuntu aarch64，需安装 ROS Humble 和 RealSense 驱动。

部署：[服务端](packaging/README-server.md)  [采集端](packaging/README-orin.md)

网页地址：`http://服务器IP:8790`。连接在线设备后，在“程序配置”中填写 DDS 主机信息。

Version:1.1.0
<img width="1922" height="1118" alt="截图 2026-11-11 11-45-19" src="https://github.com/user-attachments/assets/1dca229a-b52f-420f-acc8-f608d38ea89e" />
<img width="1922" height="1118" alt="截图 2026-11-11 11-45-39" src="https://github.com/user-attachments/assets/c2dab4a9-5c27-4652-bf22-c9e0bad6d8e8" />
<img width="1912" height="1117" alt="截图 2026-11-11 14-13-30" src="https://github.com/user-attachments/assets/eb5823ce-2749-4405-8fcc-b79c310eec2e" />
<img width="1922" height="1118" alt="截图 2026-11-11 11-46-07" src="https://github.com/user-attachments/assets/3d980acf-168e-4a6e-91ee-a128573686ed" />
<img width="1912" height="1117" alt="截图 2026-11-11 14-18-39" src="https://github.com/user-attachments/assets/519ef060-4f45-460d-9c75-57b62491b7d8" />
<img width="1912" height="1117" alt="截图 2026-11-11 14-18-23" src="https://github.com/user-attachments/assets/9ebd2f23-2689-4011-838e-9b3cfcb00d0e" />
<img width="1912" height="1117" alt="截图 2026-11-11 14-15-31" src="https://github.com/user-attachments/assets/99819bfb-720b-4a46-9050-67c0f4048f77" />
<img width="1912" height="1117" alt="截图 2026-11-11 14-14-14" src="https://github.com/user-attachments/assets/27331294-4526-4771-97cc-b260cdda18be" />
<img width="1912" height="1117" alt="截图 2026-11-11 14-13-30" src="https://github.com/user-attachments/assets/c76d7947-206c-431f-b9f2-4059d5220b4a" />


