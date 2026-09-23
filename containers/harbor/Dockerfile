# syntax=docker/dockerfile:1.7

ARG ROS_IMAGE=ros@sha256:c3706ef0a0aa45413c07803cf433602f543b22e45b4855f6fca955c2d8ecc4e8
FROM ${ROS_IMAGE}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG DEBIAN_FRONTEND=noninteractive
ARG ENTRYPLUG_UID=1000
ARG ENTRYPLUG_GID=1000

ENV PYTHONNOUSERSITE=1 \
    ROS_DISTRO=jazzy \
    RMW_IMPLEMENTATION=rmw_zenoh_cpp

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      libegl1=1.7.0-1build1 \
      libgbm1=25.2.8-0ubuntu0.24.04.2 \
      libgl1-mesa-dri=25.2.8-0ubuntu0.24.04.2 \
      libglfw3-dev=3.3.10-1build1 \
      libglx-mesa0=25.2.8-0ubuntu0.24.04.2 \
      libxi6=2:1.8.1-1build1 \
      libxkbcommon0=1.6.0-1build1 \
      mesa-utils=9.0.0-2 \
      ninja-build=1.11.1-2 \
      python3-opencv=4.6.0+dfsg-13.1ubuntu1 \
      ros-jazzy-cv-bridge=4.1.0-1noble.20260902.080510 \
      ros-jazzy-joint-trajectory-controller=4.42.1-1noble.20260905.073033 \
      ros-jazzy-mujoco-ros2-control=0.1.1-1noble.20260905.073749 \
      ros-jazzy-mujoco-ros2-control-demos=0.1.1-1noble.20260905.074828 \
      ros-jazzy-rmw-cyclonedds-cpp=2.2.4-1noble.20260902.041134 \
      ros-jazzy-rmw-zenoh-cpp=0.2.10-1noble.20260902.013525 \
      ros-jazzy-ros2-controllers=4.42.1-1noble.20260905.075205 \
      ros-jazzy-ros2controlcli=4.48.0-1noble.20260905.074402 \
      xauth=1:1.1.2-1build1 \
      xvfb=2:21.1.12-1ubuntu1.6 && \
    mkdir -p /opt/entryplug && \
    dpkg-query -W -f='${Package}\t${Version}\n' | sort > /opt/entryplug/apt-manifest.tsv

RUN existing_group="$(getent group "${ENTRYPLUG_GID}" | cut -d: -f1)" && \
    if [[ -n "${existing_group}" ]]; then \
      groupmod --new-name entryplug "${existing_group}"; \
    else \
      groupadd --gid "${ENTRYPLUG_GID}" entryplug; \
    fi && \
    existing_user="$(getent passwd "${ENTRYPLUG_UID}" | cut -d: -f1)" && \
    if [[ -n "${existing_user}" ]]; then \
      usermod --login entryplug --home /home/entryplug --move-home "${existing_user}" && \
      usermod --gid "${ENTRYPLUG_GID}" entryplug; \
    else \
      useradd --create-home --uid "${ENTRYPLUG_UID}" --gid "${ENTRYPLUG_GID}" entryplug; \
    fi

WORKDIR /workspace/entryplug
COPY --chown=entryplug:entryplug . /workspace/entryplug
RUN install -o entryplug -g entryplug -d /workspace/entryplug/runs && \
    chown entryplug:entryplug /workspace/entryplug && \
    install -o root -g root -m 0755 \
      /workspace/entryplug/containers/harbor/entrypoint.sh \
      /usr/local/bin/entryplug-container && \
    install -o root -g root -m 0755 \
      /workspace/entryplug/containers/harbor/smoke.sh \
      /usr/local/bin/entryplug-harbor-smoke

USER entryplug
ENV HOME=/home/entryplug \
    PYTHONPATH=/workspace/entryplug/src

RUN python3 -m pytest -q

ENTRYPOINT ["/usr/local/bin/entryplug-container"]
CMD ["./entryplug", "doctor", "--profile", "harbor", "--no-record"]
