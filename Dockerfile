# Note: this is a armv7 architecture specific dockerfile
# and can only be built on a raspberry pi:
#   docker build -t angadsingh/argos-presence:armv7
# or using docker buildx like so:
# setup buildx first: https://collabnix.com/building-arm-based-docker-images-on-docker-desktop-made-possible-using-buildx/
#   docker buildx build --platform linux/arm/v7 -t angadsingh/argos-presence:armv7 .

FROM arm32v7/python:3.7-slim-buster

RUN mkdir -p /usr/src/argos-presence
WORKDIR /usr/src/argos-presence

RUN apt-get update && apt-get install -y build-essential python3-dev git libjpeg62 libwebp-dev libpng-dev libtiff-dev libopenjp2.7-dev libilmbase-dev libopenexr-dev libgstreamer1.0-dev ffmpeg libgtk-3-dev libatlas3-base
RUN python3 -m venv venv
ENV PATH="/usr/src/argos-presence/venv/bin:$PATH"
RUN pip install --upgrade pip
RUN pip install wheel

# since pip isn't able to find the armv7 wheels for these packages inside docker for some reason ¯\_(ツ)_/¯

RUN pip install https://www.piwheels.org/simple/numpy/numpy-1.19.5-cp37-cp37m-linux_armv7l.whl
RUN pip install https://www.piwheels.org/simple/h5py/h5py-2.10.0-cp37-cp37m-linux_armv7l.whl
RUN pip install https://www.piwheels.org/simple/cffi/cffi-1.14.4-cp37-cp37m-linux_armv7l.whl
RUN pip install https://www.piwheels.org/simple/bcrypt/bcrypt-3.2.0-cp37-cp37m-linux_armv7l.whl
RUN pip install https://www.piwheels.org/simple/cryptography/cryptography-3.3.1-cp37-cp37m-linux_armv7l.whl
RUN pip install https://www.piwheels.org/simple/pynacl/PyNaCl-1.4.0-cp37-cp37m-linux_armv7l.whl
RUN pip install https://www.piwheels.org/simple/opencv-python/opencv_python-4.5.1.48-cp37-cp37m-linux_armv7l.whl

ENV READTHEDOCS=True
COPY ./ /usr/src/argos-presence/
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000
VOLUME /motion_frames
VOLUME /configs

ENV PYTHONPATH "${PYTHONPATH}:/configs"

ENTRYPOINT ["python3"]