#!/bin/bash
set -euo pipefail

run_dir=${1:?usage: entryplug-harbor-smoke RUN_DIRECTORY}
case_name=${2:-render-smoke}
reuse_run=${3:-}
if [[ -e "${run_dir}" ]]; then
  echo "refusing to overwrite existing evidence: ${run_dir}" >&2
  exit 2
fi
mkdir -p "${run_dir}"

declare -a owned_pids=()
cleanup() {
  local pid
  local _
  for pid in "${owned_pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill -TERM "${pid}" 2>/dev/null || true
    fi
  done
  for _ in {1..20}; do
    local any_running=false
    for pid in "${owned_pids[@]}"; do
      if kill -0 "${pid}" 2>/dev/null; then
        any_running=true
      fi
    done
    if [[ "${any_running}" == false ]]; then
      break
    fi
    sleep 0.1
  done
  for pid in "${owned_pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill -KILL "${pid}" 2>/dev/null || true
    fi
  done
  for pid in "${owned_pids[@]}"; do
    wait "${pid}" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

export DISPLAY=:99
export LIBGL_ALWAYS_SOFTWARE=1
export MUJOCO_GL=glfw
export RMW_IMPLEMENTATION=rmw_zenoh_cpp

cp /opt/entryplug/apt-manifest.tsv "${run_dir}/apt-manifest.tsv"
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp >"${run_dir}/xvfb.log" 2>&1 &
owned_pids+=("$!")

for _ in {1..50}; do
  if [[ -S /tmp/.X11-unix/X99 ]]; then
    break
  fi
  sleep 0.1
done
if [[ ! -S /tmp/.X11-unix/X99 ]]; then
  echo "Xvfb did not become ready" >&2
  exit 1
fi
glxinfo -B >"${run_dir}/renderer.txt" 2>&1

ros2 run rmw_zenoh_cpp rmw_zenohd >"${run_dir}/zenoh.log" 2>&1 &
owned_pids+=("$!")
sleep 1

if [[ "${case_name}" == mirrors ]]; then
  python3 /workspace/entryplug/containers/harbor/mirrors_launch.py \
    >"${run_dir}/mujoco.log" 2>&1 &
else
  ros2 launch mujoco_ros2_control_demos 01_basic_robot.launch.py \
    >"${run_dir}/mujoco.log" 2>&1 &
fi
owned_pids+=("$!")

ready=false
for _ in {1..12}; do
  if timeout 5 ros2 control list_controllers \
    >"${run_dir}/controllers-initial.txt" 2>&1; then
    if grep -qE '^position_controller .* active$' "${run_dir}/controllers-initial.txt"; then
      if [[ "${case_name}" != mirrors ]] || \
        grep -qE '^mirror_controller .* active$' "${run_dir}/controllers-initial.txt"; then
        ready=true
        break
      fi
    fi
  fi
  sleep 0.25
done
if [[ "${ready}" != true ]]; then
  echo "controller manager did not become ready" >&2
  exit 1
fi

ros2 control switch_controllers --deactivate position_controller --strict \
  >"${run_dir}/controller-switch.txt" 2>&1
ros2 control unload_controller position_controller \
  >"${run_dir}/controller-unload.txt" 2>&1
ros2 run controller_manager spawner trajectory_controller \
  --param-file /workspace/entryplug/containers/harbor/trajectory_controller.yaml \
  --controller-manager-timeout 30 \
  >"${run_dir}/controller-spawn.txt" 2>&1
ros2 control list_controllers >"${run_dir}/controllers-final.txt" 2>&1

if [[ "${case_name}" == mirrors ]]; then
  if ! grep -qE '^mirror_controller .* active$' "${run_dir}/controllers-final.txt"; then
    echo "mirror fixture controller did not become active" >&2
    exit 1
  fi
  python3 /workspace/entryplug/containers/harbor/mirror_driver.py \
    --evaluation-output "${run_dir}/mirror-schedule.private.jsonl" \
    >"${run_dir}/mirror-driver.log" 2>&1 &
  owned_pids+=("$!")
fi

set +e
case "${case_name}" in
  render-smoke)
    python3 /workspace/entryplug/containers/harbor/smoke.py \
      --output "${run_dir}/smoke.json"
    case_status=$?
    ;;
  reach)
    python3 /workspace/entryplug/containers/harbor/reach.py \
      --output "${run_dir}/reach.json"
    case_status=$?
    ;;
  mirrors)
    declare -a mirrors_args=(--output "${run_dir}/mirrors.json")
    if [[ -n "${reuse_run}" ]]; then
      mirrors_args+=(
        --reuse-cache
        "/workspace/entryplug/runs/${reuse_run}/evidence-cache.private.sqlite3"
      )
    fi
    python3 /workspace/entryplug/containers/harbor/mirrors.py "${mirrors_args[@]}"
    case_status=$?
    ;;
  *)
    echo "unsupported Harbor case: ${case_name}" >&2
    case_status=2
    ;;
esac
if [[ "${case_name}" == mirrors && "${case_status}" -eq 0 ]]; then
  python3 /workspace/entryplug/containers/harbor/spectator_capture.py \
    --output "${run_dir}/spectator-overview.raw.png" \
    >"${run_dir}/spectator-capture.log" 2>&1
  case_status=$?
fi
if [[ "${case_name}" == mirrors && "${case_status}" -eq 0 ]]; then
  python3 /workspace/entryplug/containers/harbor/mirrors_report.py \
    --run-dir "${run_dir}" \
    >"${run_dir}/observer-report.log" 2>&1
  case_status=$?
fi
./entryplug doctor --profile harbor --json --output "${run_dir}/doctor.json" \
  >"${run_dir}/doctor-stdout.json"
doctor_status=$?
set -e

if [[ "${case_status}" -ne 0 ]]; then
  exit "${case_status}"
fi
if [[ "${doctor_status}" -ne 0 ]]; then
  echo "Harbor case passed but its readiness profile is not qualified" >&2
  exit "${doctor_status}"
fi
