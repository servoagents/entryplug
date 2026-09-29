#!/bin/bash
set -euo pipefail

run_dir=${1:?usage: hybrid_inspection.sh RUN_DIRECTORY RUN_ID}
run_id=${2:?missing run ID}
if [[ -e "${run_dir}" ]]; then
  echo "refusing to overwrite evidence: ${run_dir}" >&2
  exit 2
fi
mkdir -p "${run_dir}"

declare -a owned_pids=()
cleanup() {
  local pid
  for pid in "${owned_pids[@]}"; do
    kill -TERM "${pid}" 2>/dev/null || true
  done
  for _ in {1..30}; do
    local any_running=false
    for pid in "${owned_pids[@]}"; do
      if kill -0 "${pid}" 2>/dev/null; then any_running=true; fi
    done
    [[ "${any_running}" == false ]] && break
    sleep 0.1
  done
  for pid in "${owned_pids[@]}"; do
    kill -KILL "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

export DISPLAY=:99
export LIBGL_ALWAYS_SOFTWARE=1
export MUJOCO_GL=glfw
export RMW_IMPLEMENTATION=rmw_zenoh_cpp

Xvfb :99 -screen 0 1280x720x24 -nolisten tcp >"${run_dir}/xvfb.log" 2>&1 &
owned_pids+=("$!")
for _ in {1..50}; do
  [[ -S /tmp/.X11-unix/X99 ]] && break
  sleep 0.1
done
[[ -S /tmp/.X11-unix/X99 ]] || exit 1

ros2 run rmw_zenoh_cpp rmw_zenohd >"${run_dir}/ros-zenoh.log" 2>&1 &
owned_pids+=("$!")
python3 /workspace/entryplug/containers/harbor/panel_launch.py \
  >"${run_dir}/mujoco.log" 2>&1 &
owned_pids+=("$!")
python3 /workspace/entryplug/containers/harbor/mqtt_lamp_bridge.py \
  --run-id "${run_id}" --broker-host broker --output "${run_dir}/bridge.json" \
  >"${run_dir}/bridge.log" 2>&1 &
owned_pids+=("$!")

python3 /workspace/entryplug/containers/mixed/hybrid_inspection.py \
  --output "${run_dir}/hybrid.json" --run-id "${run_id}"
