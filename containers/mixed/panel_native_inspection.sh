#!/bin/bash
set -euo pipefail

run_dir=${1:?usage: panel_native_inspection.sh RUN_DIRECTORY RUN_ID}
run_id=${2:?missing run ID}
if [[ -e "${run_dir}" ]]; then
  echo "refusing to overwrite evidence: ${run_dir}" >&2
  exit 2
fi
mkdir -p "${run_dir}"

declare -a owned_pids=()
cleanup() {
  local pid
  local any_running
  for pid in "${owned_pids[@]}"; do
    kill -TERM "${pid}" 2>/dev/null || true
  done
  for _ in {1..30}; do
    any_running=false
    for pid in "${owned_pids[@]}"; do
      if kill -0 "${pid}" 2>/dev/null; then
        any_running=true
      fi
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
export PYTHONPATH="/workspace/entryplug/containers/harbor:${PYTHONPATH:-}"

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

for index in a b; do
  if [[ "${index}" == a ]]; then port=7448; else port=7449; fi
  python3 /workspace/entryplug/containers/mixed/native_detector_worker.py \
    --run-id "${run_id}" --worker-id "worker-${index}" --port "${port}" \
    --ready "${run_dir}/worker-${index}.ready" \
    --report "${run_dir}/worker-${index}.json" \
    >"${run_dir}/worker-${index}.log" 2>&1 &
  owned_pids+=("$!")
done
for _ in {1..80}; do
  [[ -f "${run_dir}/worker-a.ready" && -f "${run_dir}/worker-b.ready" ]] && break
  sleep 0.1
done
if [[ ! -f "${run_dir}/worker-a.ready" || ! -f "${run_dir}/worker-b.ready" ]]; then
  echo "native workers did not become ready" >&2
  exit 1
fi

python3 /workspace/entryplug/containers/mixed/panel_native_inspection.py \
  --output "${run_dir}/panel-native-inspection.json" --run-id "${run_id}"
