#!/bin/bash
set -eo pipefail

source /opt/ros/jazzy/setup.bash
source /opt/entryplug/fixture-lamp/setup.bash
set -u
export PYTHONNOUSERSITE=1

exec "$@"
