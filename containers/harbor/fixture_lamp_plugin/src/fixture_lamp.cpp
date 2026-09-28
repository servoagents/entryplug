// Fixture-only lighting hook. No network callback writes to MuJoCo directly.

#include <atomic>
#include <cmath>
#include <cstdint>
#include <memory>

#include <mujoco/mujoco.h>
#include <pluginlib/class_list_macros.hpp>
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/float32.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>

#include "mujoco_ros2_control_plugins/mujoco_ros2_control_plugins_base.hpp"

namespace entryplug_fixture_lamp
{

class FixtureLamp final : public mujoco_ros2_control_plugins::MuJoCoROS2ControlPluginBase
{
public:
  bool init(rclcpp::Node::SharedPtr node, const mjModel* model, mjData*) override
  {
    const int light_id = mj_name2id(model, mjOBJ_LIGHT, "fixture_lamp");
    if (light_id < 0) {
      RCLCPP_ERROR(node->get_logger(), "fixture_lamp was not found in the MuJoCo model");
      return false;
    }
    // MuJoCo permits runtime changes to these visual parameters. Only pre_step()
    // writes them, while the simulation mutex is held by the world owner.
    model_ = const_cast<mjModel*>(model);
    light_id_ = light_id;
    for (int channel = 0; channel < 3; ++channel) {
      base_diffuse_[channel] = model->light_diffuse[3 * light_id + channel];
      base_ambient_[channel] = model->light_ambient[3 * light_id + channel];
    }
    state_ = node->create_publisher<std_msgs::msg::Float64MultiArray>(
      "/entryplug_fixture_lamp/state", rclcpp::QoS(1));
    command_ = node->create_subscription<std_msgs::msg::Float32>(
      "/entryplug_fixture_lamp/command", rclcpp::QoS(1),
      [this](const std_msgs::msg::Float32::SharedPtr message) {
        const float level = message->data;
        if (!std::isfinite(level) || level < 0.0F || level > 0.75F) {
          return;
        }
        requested_level_.store(level, std::memory_order_relaxed);
        requested_revision_.fetch_add(1, std::memory_order_release);
      });
    // Start dark in this dedicated fixture. Normal mirror runs do not load this plugin.
    requested_revision_.store(1, std::memory_order_release);
    return true;
  }

  void pre_step(mjData* data) override
  {
    const auto revision = requested_revision_.load(std::memory_order_acquire);
    if (revision == applied_revision_.load(std::memory_order_relaxed)) {
      return;
    }
    const float level = requested_level_.load(std::memory_order_relaxed);
    for (int channel = 0; channel < 3; ++channel) {
      model_->light_diffuse[3 * light_id_ + channel] = base_diffuse_[channel] * level;
      model_->light_ambient[3 * light_id_ + channel] = base_ambient_[channel] * level;
    }
    applied_level_.store(level, std::memory_order_release);
    applied_sim_time_.store(data->time, std::memory_order_release);
    applied_revision_.store(revision, std::memory_order_release);
  }

  void update(const mjModel*, mjData*) override
  {
    if (applied_revision_.load(std::memory_order_acquire) == 0) {
      return;
    }
    // Repeat the applied value so a late subscriber can bootstrap its state.
    if (++update_count_ % 10 != 0) {
      return;
    }
    std_msgs::msg::Float64MultiArray message;
    message.data = {
      applied_level_.load(std::memory_order_acquire),
      applied_sim_time_.load(std::memory_order_acquire),
      static_cast<double>(applied_revision_.load(std::memory_order_acquire))};
    state_->publish(message);
  }

  void cleanup() override
  {
    command_.reset();
    state_.reset();
    model_ = nullptr;
  }

private:
  mjModel* model_ = nullptr;
  int light_id_ = -1;
  float base_diffuse_[3] = {};
  float base_ambient_[3] = {};
  std::atomic<float> requested_level_{0.0F};
  std::atomic<float> applied_level_{0.0F};
  std::atomic<double> applied_sim_time_{0.0};
  std::atomic<std::uint64_t> requested_revision_{0};
  std::atomic<std::uint64_t> applied_revision_{0};
  std::uint64_t update_count_ = 0;
  rclcpp::Subscription<std_msgs::msg::Float32>::SharedPtr command_;
  rclcpp::Publisher<std_msgs::msg::Float64MultiArray>::SharedPtr state_;
};

}  // namespace entryplug_fixture_lamp

PLUGINLIB_EXPORT_CLASS(
  entryplug_fixture_lamp::FixtureLamp,
  mujoco_ros2_control_plugins::MuJoCoROS2ControlPluginBase)
