cmake_minimum_required(VERSION 3.8)
project(entryplug_fixture_lamp)

find_package(ament_cmake REQUIRED)
find_package(mujoco_ros2_control_plugins REQUIRED)
find_package(pluginlib REQUIRED)
find_package(rclcpp REQUIRED)
find_package(std_msgs REQUIRED)

add_library(entryplug_fixture_lamp SHARED src/fixture_lamp.cpp)
target_compile_features(entryplug_fixture_lamp PUBLIC cxx_std_17)
ament_target_dependencies(
  entryplug_fixture_lamp
  mujoco_ros2_control_plugins
  pluginlib
  rclcpp
  std_msgs
)
pluginlib_export_plugin_description_file(mujoco_ros2_control_plugins plugins.xml)
install(TARGETS entryplug_fixture_lamp LIBRARY DESTINATION lib)

ament_package()
