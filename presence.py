import logging
import sys

from detection.motion_detector import SimpleMotionDetector
from input.picamstream import PiVideoStream
from lib.singleton_q import SingletonBlockingQueue

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
log = logging.getLogger(__name__)

log.info("package import START")
import argparse
import base64
import datetime
import importlib
import io
import json
import threading
import time

import cv2
import numpy
import requests
from flask import Flask
from flask import Response
from flask import jsonify
from flask import render_template
from flask import request
from flask_classful import FlaskView, route

from lib.fps import FPS
from lib.ha_mqtt import HaMQTT
from lib.timer import RepeatedTimer

log.info("package import END")


class PresenceDetector():
    def __init__(self, config, camconfig):
        self.config = config
        self.camconfig = camconfig
        self.outputFrame = SingletonBlockingQueue()
        self.motion_status = 0
        self.motion_status_changed = False
        self.last_motion_ts = datetime.datetime.now()
        self.last_nonmotion_ts = datetime.datetime.now()
        self.active_video_feeds = 0
        self.mqtt = HaMQTT(self.config.mqtt_host, self.config.mqtt_port,
                           self.config.mqtt_username, self.config.mqtt_password)
        self.stopped = False
        if config.send_mqtt:
            self.mqtt_heartbeat_timer = RepeatedTimer(self.config.mqtt_heartbeat_secs, self.mqtt_heartbeat)

    def set_cam_config(self):
        for key, val in vars(self.camconfig).items():
            setattr(self.vs.camera, key, val)

    def start(self):
        # start the pi video stream thread
        self.vs = PiVideoStream(format='bgr').start()
        self.set_cam_config()

        # start a thread that will perform motion detection
        self.md_thread = threading.Thread(target=self.detect_motion)
        self.md_thread.daemon = True
        self.md_thread.start()
        return self.vs.t

    def cleanup(self):
        self.stopped = True
        self.md_thread.join()
        self.mqtt_heartbeat_timer.stop()
        self.vs.stop()

    def mqtt_heartbeat(self):
        self.mqtt.publish(self.config.mqtt_state_topic, self.motion_status)

    def detect_person(self, frame):
        content_type = 'image/jpeg'
        is_success, buffer = cv2.imencode(".jpg", frame)
        img_bytes = io.BytesIO(buffer)
        det_boxes = None
        url = self.config.argos_service_api_url + '?threshold=%s' % str(self.config.argos_detection_threshold)
        if self.config.argos_detection_nmask:
            enc_mask = base64.urlsafe_b64encode(json.dumps(self.config.argos_detection_nmask).encode()).decode()
            url = url + '&nmask=%s' % enc_mask
        try:
            det_boxes = requests.post(url, files={'file': ('presence_detector_%s' % int(time.time()),
                                                           img_bytes, content_type)}).json()
        except Exception as e:
            log.error("Could not contact argos object detection service", e)

        if det_boxes is not None:
            if len(det_boxes) > 0:
                for box in det_boxes:
                    minx, miny, maxx, maxy, label, accuracy = box
                    if label == 'person':
                        log.info("argosDetector person found: %s" % str(box))
                        return box
        return False

    def detect_presence(self, frame, motion, total_frames):
        person_box = None
        self.motion_status_changed = False

        if motion is not None:
            if self.motion_status == 0:
                if (datetime.datetime.now() - self.last_nonmotion_ts).total_seconds() \
                        <= self.config.presence_cool_down_secs:
                    if self.config.argos_person_detection_enabled:
                        # do person detection here and dont reset bg (let motion come)
                        # only activate to motion state if person found
                        log.info("coolDown: detecting person (%d)" % (
                                datetime.datetime.now() - self.last_nonmotion_ts).total_seconds())
                        person_box = self.detect_person(frame)
                        if person_box:
                            log.info("coolDown aborted: person detected")
                            self.motion_status = 1
                            self.motion_status_changed = True
                            log.info("motionStatus: %d" % self.motion_status)
                    else:
                        # reset the background model to account for motion
                        # following a status change to non motion (e.g. lighting going off)
                        self.config.reset_bg_model = True
                else:
                    self.motion_status = 1
                    self.motion_status_changed = True
                    log.info("motionStatus: %d" % self.motion_status)
            self.last_motion_ts = datetime.datetime.now()
        else:
            if self.motion_status == 1:
                if (datetime.datetime.now() - self.last_motion_ts).total_seconds() \
                        > self.config.presence_idle_secs:
                    self.motion_status = 0
                    self.motion_status_changed = True
                    self.last_nonmotion_ts = datetime.datetime.now()
                    log.info("motionStatus: %d" % self.motion_status)
                else:
                    if self.config.argos_person_detection_enabled:
                        # do person detection here
                        # if person found, update last_motion_ts
                        if total_frames % self.config.argos_detection_frequency_frames == 0:
                            log.info("nonMotion: detecting person (%d)" % (
                                    datetime.datetime.now() - self.last_motion_ts).total_seconds())
                            person_box = self.detect_person(frame)
                            if person_box:
                                self.last_motion_ts = datetime.datetime.now()

        if self.motion_status_changed:
            if self.config.send_mqtt:
                self.mqtt.publish(self.config.mqtt_state_topic, self.motion_status)

        return person_box

    def detect_motion(self):
        # initialize the motion detector and the total number of frames
        # read thus far
        md = SimpleMotionDetector(config)
        total = 0

        fps = FPS(50, 100)

        # loop over frames from the video stream
        while not self.stopped:
            # read the next frame from the video stream, resize it,
            # convert the frame to grayscale, and blur it
            frame = self.vs.read()
            fps.count()
            total += 1

            # detect motion in the image
            (frame, crop, motion_outside) = md.detect(frame)
            person_box = self.detect_presence(frame, crop, total)
            if person_box:
                minx, miny, maxx, maxy, label, accuracy = person_box
                text = label + ": " + str(numpy.round(accuracy, 2))
                cv2.rectangle(frame, (minx, miny), (maxx, maxy), (0, 255, 0), 2)
                cv2.putText(frame, text, (minx + 5, miny - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

            if total % self.config.fps_print_frames == 0:
                log.info("fps: %.2f" % fps.fps)

            # grab the current timestamp and draw it on the frame
            if self.config.show_fps:
                cv2.putText(frame, "%.2f fps" % fps.fps, (10, frame.shape[0] - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 255), 1)
            self.outputFrame.enqueue(frame.copy())

    def generate(self):
        self.active_video_feeds += 1
        # loop over frames from the output stream
        try:
            while True:
                if self.config.video_feed_fps > 0:
                    time.sleep(1 / min(int(self.vs.camera.framerate), self.config.video_feed_fps))
                outputFrame = self.outputFrame.read()
                # check if the output frame is available, otherwise skip
                # the iteration of the loop
                if outputFrame is None:
                    continue
                # encode the frame in JPEG format
                (flag, encodedImage) = cv2.imencode(".jpg", outputFrame)
                # ensure the frame was successfully encoded
                if not flag:
                    continue
                # yield the output frame in the byte format
                yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' +
                       bytearray(encodedImage) + b'\r\n')
        finally:
            self.active_video_feeds -= 1


class PresenceDetectorView(FlaskView):
    def __init__(self, presence_detector: PresenceDetector):
        super().__init__()
        self.pd = presence_detector
        self.config = self.pd.config

    @route("/")
    def index(self):
        return render_template("index.html")

    @route('/status')
    def status(self):
        return jsonify(
            {
                'active_video_feeds': self.active_video_feeds,
                'motion_status': self.motion_status,
                'motion_status_changed': self.motion_status_changed,
                'last_motion_ts': self.last_motion_ts,
                'last_nonmotion_ts': self.last_nonmotion_ts
            }
        )

    @route('/config')
    def apiconfig(self):
        self.config.show_fps = bool(request.args.get('show_fps', self.config.show_fps))
        self.config.md_show_all_contours = bool(request.args.get('md_show_all_contours', self.config.md_show_all_contours))
        self.config.md_update_bg_model = bool(request.args.get('md_update_bg_model', self.config.md_update_bg_model))
        self.config.md_tval = int(request.args.get('md_tval', self.config.md_tval))
        self.config.md_min_cont_area = int(request.args.get('md_min_cont_area', self.config.md_min_cont_area))
        self.config.md_enable_erode = bool(request.args.get('md_enable_erode', self.config.md_enable_erode))
        self.config.md_enable_dilate = bool(request.args.get('md_enable_dilate', self.config.md_enable_dilate))
        self.config.md_erode_iterations = int(request.args.get('md_erode_iterations', self.config.md_erode_iterations))
        self.config.md_dilate_iterations = int(request.args.get('md_dilate_iterations', self.config.md_dilate_iterations))
        self.config.md_bg_accum_weight = float(request.args.get('md_bg_accum_weight', self.config.md_bg_accum_weight))
        self.config.md_reset_bg_model = bool(request.args.get('md_reset_bg_model', self.config.md_reset_bg_model))
        self.config.video_feed_fps = int(request.args.get('video_feed_fps', self.config.video_feed_fps))
        self.config.send_mqtt = bool(request.args.get('send_mqtt', self.config.send_mqtt))
        self.config.fps_print_frames = int(request.args.get('fps_print_frames', self.config.fps_print_frames))
        self.config.mqtt_heartbeat_secs = int(request.args.get('mqtt_heartbeat_secs', self.config.mqtt_heartbeat_secs))
        self.config.presence_idle_secs = int(request.args.get('presence_idle_secs', self.config.presence_idle_secs))
        self.config.presence_cool_down_secs = int(
            request.args.get('presence_cool_down_secs', self.config.presence_cool_down_secs))
        self.config.argos_detection_frequency_frames = int(
            request.args.get('argos_person_detection_enabled', self.config.argos_person_detection_enabled))
        self.config.argos_detection_threshold = float(
            request.args.get('argos_detection_threshold', self.config.argos_detection_threshold))
        self.config.argos_detection_frequency_frames = int(
            request.args.get('argos_detection_frequency_frames', self.config.argos_detection_frequency_frames))

        return jsonify(self.config.__dict__)

    @route('/camconfig')
    def camconfig(self):
        self.pd.vs.camera.exposure_mode = request.args.get('exposure_mode', self.pd.vs.camera.exposure_mode)
        self.pd.vs.camera.framerate = int(request.args.get('framerate', self.pd.vs.camera.framerate))
        self.pd.vs.camera.iso = int(request.args.get('iso', self.pd.vs.camera.iso))
        self.pd.vs.camera.meter_mode = request.args.get('meter_mode', self.pd.vs.camera.meter_mode)

        red, blue = self.pd.vs.camera.awb_gains
        red = float(request.args.get('awb_gains_red', red))
        blue = float(request.args.get('awb_gains_blue', blue))
        self.pd.vs.camera.awb_gains = (red, blue)

        self.pd.vs.camera.awb_mode = request.args.get('awb_mode', self.pd.vs.camera.awb_mode)

        self.pd.vs.camera.brightness = int(request.args.get('brightness', self.pd.vs.camera.brightness))
        self.pd.vs.camera.contrast = int(request.args.get('contrast', self.pd.vs.camera.contrast))
        self.pd.vs.camera.drc_strength = request.args.get('drc_strength', self.pd.vs.camera.drc_strength)
        self.pd.vs.camera.exposure_compensation = int(
            request.args.get('exposure_compensation', self.pd.vs.camera.exposure_compensation))
        self.pd.vs.camera.image_denoise = bool(request.args.get('image_denoise', self.pd.vs.camera.image_denoise))

        x, y = self.pd.vs.camera.resolution
        x = request.args.get('resolution_x', x)
        y = request.args.get('resolution_y', y)
        self.pd.vs.camera.resolution = (x, y)

        self.pd.vs.camera.saturation = int(request.args.get('saturation', self.pd.vs.camera.saturation))
        self.pd.vs.camera.sharpness = int(request.args.get('sharpness', self.pd.vs.camera.sharpness))
        self.pd.vs.camera.shutter_speed = int(request.args.get('shutter_speed', self.pd.vs.camera.shutter_speed))
        self.pd.vs.camera.video_denoise = bool(request.args.get('video_denoise', self.pd.vs.camera.video_denoise))
        self.pd.vs.camera.video_stabilization = bool(
            request.args.get('video_stabilization', self.pd.vs.camera.video_stabilization))

        cam_conf = {
            'exposure_mode': self.pd.vs.camera.exposure_mode,
            'framerate': float(self.pd.vs.camera.framerate),
            'iso': self.pd.vs.camera.iso,
            'meter_mode': self.pd.vs.camera.meter_mode,
            'analog_gain': float(self.pd.vs.camera.analog_gain),
            'digital_gain': float(self.pd.vs.camera.digital_gain),
            'awb_gains': (red, blue),
            'awb_mode': self.pd.vs.camera.awb_mode,
            'brightness': self.pd.vs.camera.brightness,
            'contrast': self.pd.vs.camera.contrast,
            'drc_strength': self.pd.vs.camera.drc_strength,
            'exposure_compensation': self.pd.vs.camera.exposure_compensation,
            'exposure_speed': self.pd.vs.camera.exposure_speed,
            'image_denoise': self.pd.vs.camera.image_denoise,
            'resolution': self.pd.vs.camera.resolution,
            'saturation': self.pd.vs.camera.saturation,
            'sharpness': self.pd.vs.camera.sharpness,
            'shutter_speed': self.pd.vs.camera.shutter_speed,
            'video_denoise': self.pd.vs.camera.video_denoise,
            'video_stabilization': self.pd.vs.camera.video_stabilization
        }

        return jsonify(cam_conf)

    @route("/image")
    def image(self):
        (flag, encodedImage) = cv2.imencode(".jpg", self.pd.outputFrame.read())
        return Response(bytearray(encodedImage),
                        mimetype='image/jpeg')

    @route("/video_feed")
    def video_feed(self):
        return Response(self.pd.generate(),
                        mimetype="multipart/x-mixed-replace; boundary=frame")


if __name__ == '__main__':
    # construct the argument parser and parse command line arguments
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--ip", type=str, required=True,
                    help="ip address of the device")
    ap.add_argument("-o", "--port", type=int, required=True,
                    help="ephemeral port number of the server (1024 to 65535)")
    ap.add_argument("-c", "--config", type=str, required=True,
                    help="path to the python config file")
    ap.add_argument("-y", "--camconfig", type=str, required=True,
                    help="path to the python config file for the picamera")
    args = vars(ap.parse_args())

    m = importlib.import_module(args["config"])
    config = getattr(m, "Config")()
    m2 = importlib.import_module(args["camconfig"])
    camconfig = getattr(m2, "CamConfig")()
    pd = PresenceDetector(config, camconfig)
    cam_thread = pd.start()

    # start the flask app
    app = Flask(__name__)
    PresenceDetectorView.register(app, init_argument=pd, route_base='/')
    flask_thread = threading.Thread(target=app.run, kwargs={'host': args["ip"], 'port': args["port"], 'debug': False,
                                                            'threaded': True, 'use_reloader': False})
    flask_thread.daemon = True
    flask_thread.start()

    cam_thread.join()
    pd.cleanup()
