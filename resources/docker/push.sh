#!/bin/bash

docker buildx build --platform linux/arm/v6 -t angadsingh/argos-presence:armv6 -f resources/docker/Dockerfile_armv6 . --push
docker buildx build --platform linux/arm/v7 -t angadsingh/argos-presence:armv7 -f resources/docker/Dockerfile_armv7 . --push