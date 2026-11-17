#pragma once

// Copyright (c) 2026 deeprobotics
// Author Chenyu Guo
// Date November 2026

#include <cstdlib>
#include <filesystem>

namespace humanoid {

inline std::filesystem::path RecordingRoot() {
  const char* configured = std::getenv("ARMDOG_DATA_ROOT");
  return configured && *configured
      ? std::filesystem::absolute(configured).lexically_normal()
      : std::filesystem::current_path() / "data";
}

}  // namespace humanoid
