#pragma once

// Copyright (c) 2026 deeprobotics
// Author Chenyu Guo
// Date November 2026

#include "camera_assignment.h"

#include <librealsense2/rs.hpp>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <unistd.h>

namespace utils {

inline std::vector<CameraDevice> DiscoverRealSenseCameras() {
  std::vector<CameraDevice> devices;
  try {
    rs2::context context;
    for (auto&& device : context.query_devices()) {
      if (!device.supports(RS2_CAMERA_INFO_SERIAL_NUMBER)) continue;
      CameraDevice camera{device.get_info(RS2_CAMERA_INFO_SERIAL_NUMBER), ""};
      if (device.supports(RS2_CAMERA_INFO_PHYSICAL_PORT)) {
        camera.port = NormalizeCameraPort(
            device.get_info(RS2_CAMERA_INFO_PHYSICAL_PORT));
      }
      devices.push_back(std::move(camera));
    }
  } catch (const std::exception& error) {
    std::cerr << "[ArmDogRecorder] camera discovery failed: "
              << error.what() << '\n';
  }
  return devices;
}

inline CameraRoles ResolveRealSenseCameraRoles(
    const std::vector<CameraDevice>& devices) {
  const char* override = std::getenv("ARMDOG_CAMERA_PORTS_FILE");
  std::error_code error;
  const auto executable = std::filesystem::read_symlink("/proc/self/exe", error);
  const auto path = override && *override
      ? std::filesystem::path(override)
      : executable.parent_path().parent_path() / "camera_ports.conf";
  CameraPorts remembered{};
  std::ifstream input(path);
  std::string line;
  while (std::getline(input, line)) {
    const auto separator = line.find('=');
    if (separator == std::string::npos) continue;
    for (size_t i = 0; i < kCameraRoles.size(); ++i) {
      if (line.substr(0, separator) == kCameraRoles[i]) {
        remembered[i] = NormalizeCameraPort(line.substr(separator + 1));
      }
    }
  }
  const auto roles = AssignCameraRoles(devices, remembered);
  auto updated = remembered;
  for (size_t i = 0; i < roles.size(); ++i) {
    if (roles[i].serial.empty()) {
      std::cerr << "[ArmDogRecorder] camera role=" << kCameraRoles[i]
                << " unavailable\n";
      continue;
    }
    if (!roles[i].port.empty()) updated[i] = roles[i].port;
    std::cout << "[ArmDogRecorder] camera role=" << kCameraRoles[i]
              << " port=" << roles[i].port << " serial=" << roles[i].serial
              << '\n';
  }
  if (updated != remembered && (!error || (override && *override))) {
    const auto temporary = path.string() + ".tmp." + std::to_string(getpid());
    std::ofstream output(temporary);
    for (size_t i = 0; i < kCameraRoles.size(); ++i) {
      output << kCameraRoles[i] << '=' << updated[i] << '\n';
    }
    output.close();
    if (output) std::filesystem::rename(temporary, path, error);
    if (!output || error) {
      std::cerr << "[ArmDogRecorder] cannot save camera port mapping: "
                << path << '\n';
      std::filesystem::remove(temporary, error);
    }
  }
  return roles;
}

}  // namespace utils
