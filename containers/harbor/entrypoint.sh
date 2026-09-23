#!/bin/bash
set -eo pipefail

source /opt/ros/jazzy/setup.bash
set -u
export PYTHONNOUSERSITE=1

exec "$@"
