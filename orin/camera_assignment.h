#pragma once

// Copyright (c) 2026 deeprobotics
// Author Chenyu Guo
// Date November 2026

#include <algorithm>
#include <array>
#include <string>
#include <vector>

namespace utils {

struct CameraDevice {
  std::string serial;
  std::string port;
};

using CameraPorts = std::array<std::string, 3>;
using CameraRoles = std::array<CameraDevice, 3>;
inline constexpr std::array<const char*, 3> kCameraRoles{
    "primary", "third", "d435i"};

inline std::string NormalizeCameraPort(std::string port) {
  const auto video = port.find("/video4linux/");
  if (video != std::string::npos) port.erase(video);
  const auto interface = port.find(':', port.find_last_of('/') + 1);
  if (interface != std::string::npos) port.erase(interface);
  return port;
}

inline CameraRoles AssignCameraRoles(std::vector<CameraDevice> devices,
                                    const CameraPorts& remembered) {
  for (auto& device : devices) device.port = NormalizeCameraPort(device.port);
  std::stable_sort(devices.begin(), devices.end(),
                   [](const auto& a, const auto& b) { return a.port < b.port; });
  std::vector<CameraDevice> unique;
  for (const auto& device : devices) {
    if (device.serial.empty()) continue;
    const auto same_device = [&](const auto& other) {
      return other.serial == device.serial ||
             (!device.port.empty() && other.port == device.port);
    };
    if (std::none_of(unique.begin(), unique.end(), same_device)) {
      unique.push_back(device);
    }
  }
  CameraRoles roles{};
  std::vector<bool> used(unique.size(), false);
  for (size_t role = 0; role < roles.size(); ++role) {
    const auto port = NormalizeCameraPort(remembered[role]);
    if (port.empty()) continue;
    for (size_t i = 0; i < unique.size(); ++i) {
      if (!used[i] && unique[i].port == port) {
        roles[role] = unique[i];
        used[i] = true;
        break;
      }
    }
  }
  for (size_t i = 0; i < unique.size(); ++i) {
    if (used[i]) continue;
    for (auto& role : roles) {
      if (role.serial.empty()) {
        role = unique[i];
        break;
      }
    }
  }
  return roles;
}

}  // namespace utils
