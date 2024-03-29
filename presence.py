import logging
import sys

from lib.constants import InputMode
from detection.motion_detector import SimpleMotionDetector
from input import setup_input_stream

from lib.framelimiter import FrameLimiter
from lib.ha_webhook import HaWebHook
from lib.task_queue import NonBlockingTaskSingleton

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
        self.outputFrame = NonBlockingTaskSingleton()
        self.current_log_line = ""
        self.presence_status = 0
        self.presence_status_changed = False
        self.last_motion_ts = datetime.datetime.now()
        self.last_nonmotion_ts = datetime.datetime.now()
        self.active_video_feeds = 0

        self.stopped = False
        if config.argos_detection_nmask_template:
            self.nmask_detection_template = cv2.imread(config.argos_detection_nmask_template, 0)
            self.argos_detection_nmask = None
        elif self.config.argos_detection_nmask:
            self.argos_detection_nmask = self.config.argos_detection_nmask

        if config.send_mqtt:
            self.mqtt = HaMQTT(self.config.mqtt_host, self.config.mqtt_port,
                               self.config.mqtt_username, self.config.mqtt_password)
            self.mqtt_heartbeat_timer = RepeatedTimer(self.config.mqtt_heartbeat_secs, self.mqtt_heartbeat)
        if config.send_webhook:
            self.ha_webhook = HaWebHook(self.config.ha_webhook_url)

    def log(self, msg):
        log.info(msg)
        self.current_log_line = msg

    def set_cam_config(self):
        if self.config.input_mode == InputMode.PI_CAM:
            for key, val in vars(self.camconfig).items():
                setattr(self.vs.camera, key, val)

    def start(self):
        # start the pi video stream thread
        self.vs = setup_input_stream(self.config)
        self.set_cam_config()

        # start a thread that will perform motion detection
        self.md_thread = threading.Thread(target=self.detect_motion)
        self.md_thread.daemon = True
        self.md_thread.start()
        return self.vs.t

    def cleanup(self):
        self.stopped = True
        self.md_thread.join()
        if config.send_mqtt:
            self.mqtt_heartbeat_timer.stop()
        self.vs.stop()

    def mqtt_heartbeat(self):
        self.mqtt.publish(self.config.mqtt_state_topic, self.presence_status)

    def update_argos_nmask(self, frame):
        img = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        try:
            res = cv2.matchTemplate(img, self.nmask_detection_template, cv2.TM_CCOEFF_NORMED)
        except Exception as e:
            log.error("could not detect argos nmask", e)
        else:
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            w, h = self.nmask_detection_template.shape[::-1]
            top_left = max_loc
            bottom_right = (top_left[0] + w, top_left[1] + h)
            self.argos_detection_nmask = (top_left[0]-10, top_left[1]-10, bottom_right[0]+10, bottom_right[1]+10)

            if self.config.argos_show_detection_masks:
                nminX, nminY, nmaxX, nmaxY = self.argos_detection_nmask
                cv2.rectangle(frame, (nminX, nminY), (nmaxX, nmaxY), (128, 0, 128), 1)
            self.log(f"argos person detection nmask: {self.argos_detection_nmask}")

    def detect_person(self, frame):
        content_type = 'image/jpeg'
        is_success, buffer = cv2.imencode(".jpg", frame)
        img_bytes = io.BytesIO(buffer)
        det_boxes = None
        url = self.config.argos_service_api_url + '?threshold=%s' % str(self.config.argos_detection_threshold)
        if self.argos_detection_nmask:
            if self.config.argos_show_detection_masks:
                nminX, nminY, nmaxX, nmaxY = self.argos_detection_nmask
                cv2.rectangle(frame, (nminX, nminY), (nmaxX, nmaxY), (128, 0, 128), 1)
            enc_mask = base64.urlsafe_b64encode(json.dumps(self.argos_detection_nmask).encode()).decode()
            url = url + '&nmask=%s' % enc_mask
        try:
            det_boxes = requests.post(url, files={'file': ('presence_detector_%s' % int(time.time()),
                                                           img_bytes, content_type)}).json()
        except Exception as e:
            log.error("Could not contact argos object detection service {}", e)

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
        self.presence_status_changed = False

        if motion is not None:
            if self.presence_status == 0:
                if (datetime.datetime.now() - self.last_nonmotion_ts).total_seconds() \
                        <= self.config.presence_warmup_secs:
                    if self.config.argos_person_detection_enabled:
                        # do person detection here and dont reset bg (let motion come)
                        # only activate to motion state if person found
                        self.log("warmUp: detecting person (%d)" % (
                                datetime.datetime.now() - self.last_nonmotion_ts).total_seconds())
                        person_box = self.detect_person(frame)
                        if person_box:
                            self.log("warmUp aborted: person detected")
                            self.presence_status = 1
                            self.presence_status_changed = True
                            self.log("presenceStatus: %d" % self.presence_status)
                    else:
                        # reset the background model to account for motion
                        # following a status change to non motion (e.g. lighting going off)
                        self.config.reset_bg_model = True
                else:
                    self.presence_status = 1
                    self.presence_status_changed = True
                    self.log("presenceStatus: %d" % self.presence_status)
                    if self.config.md_first_frame_write:
                        image_path = "%s/motion_frame_%s.jpg" % (
                        self.config.md_first_frame_write_path, datetime.datetime.now().strftime("%d-%m-%Y-%H-%M-%S"))
                        cv2.imwrite(image_path,
                                    frame)
            self.last_motion_ts = datetime.datetime.now()
        else:
            if self.presence_status == 1:
                if (datetime.datetime.now() - self.last_motion_ts).total_seconds() \
                        > self.config.presence_cooldown_secs:
                    self.presence_status = 0
                    self.presence_status_changed = True
                    self.last_nonmotion_ts = datetime.datetime.now()
                    self.log("presenceStatus: %d" % self.presence_status)
                else:
                    if self.config.argos_person_detection_enabled:
                        # do person detection here
                        # if person found, update last_motion_ts
                        if total_frames % self.config.argos_detection_frequency_frames == 0:
                            self.log("coolDown: detecting person (%d)" % (
                                    datetime.datetime.now() - self.last_motion_ts).total_seconds())
                            person_box = self.detect_person(frame)
                            if person_box:
                                self.last_motion_ts = datetime.datetime.now()

        if self.presence_status_changed:
            if self.config.send_mqtt:
                self.mqtt.publish(self.config.mqtt_state_topic, self.presence_status)
            if self.config.send_webhook:
                self.ha_webhook.send(str(self.presence_status))

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
            md.show_masks(frame)
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
                cv2.putText(frame, "%.2f fps" % fps.fps, (frame.shape[1]-50, 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 255), 1)
            if self.config.show_status:
                cv2.putText(frame, f"presence: {self.presence_status}", (5, 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 0), 1)
                if self.current_log_line:
                    cv2.putText(frame, self.current_log_line, (5, frame.shape[0] - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 0, 0), 1)
                    self.current_log_line = ""

            # update argos person detection nmask
            if self.config.argos_detection_nmask_template:
                if total % self.config.argos_detection_nmask_template_update_freq_frames == 0:
                    self.update_argos_nmask(frame)

            if self.config.output_frame_enabled:
                self.outputFrame.enqueue(frame.copy())

    def generate(self):
        self.active_video_feeds += 1
        # loop over frames from the output stream
        try:
            frame_rate = self.config.video_feed_fps
            if self.config.input_mode == InputMode.PI_CAM:
                frame_rate = min(int(self.vs.camera.framerate), self.config.video_feed_fps)
            limiter = FrameLimiter(frame_rate)

            while limiter.limit():
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
                'active_video_feeds': self.pd.active_video_feeds,
                'presence_status': self.pd.presence_status,
                'presence_status_changed': self.pd.presence_status_changed,
                'last_motion_ts': self.pd.last_motion_ts,
                'last_nonmotion_ts': self.pd.last_nonmotion_ts,
                'argos_detection_nmask': self.pd.argos_detection_nmask
            }
        )

    @route('/config')
    def apiconfig(self):
        self.config.show_fps = bool(request.args.get('show_fps', self.config.show_fps))
        self.config.md_show_all_contours = bool(
            request.args.get('md_show_all_contours', self.config.md_show_all_contours))
        self.config.md_update_bg_model = bool(request.args.get('md_update_bg_model', self.config.md_update_bg_model))
        self.config.md_tval = int(request.args.get('md_tval', self.config.md_tval))
        self.config.md_min_cont_area = int(request.args.get('md_min_cont_area', self.config.md_min_cont_area))
        self.config.md_enable_erode = bool(request.args.get('md_enable_erode', self.config.md_enable_erode))
        self.config.md_enable_dilate = bool(request.args.get('md_enable_dilate', self.config.md_enable_dilate))
        self.config.md_erode_iterations = int(request.args.get('md_erode_iterations', self.config.md_erode_iterations))
        self.config.md_dilate_iterations = int(
            request.args.get('md_dilate_iterations', self.config.md_dilate_iterations))
        self.config.md_bg_accum_weight = float(request.args.get('md_bg_accum_weight', self.config.md_bg_accum_weight))
        self.config.md_reset_bg_model = bool(request.args.get('md_reset_bg_model', self.config.md_reset_bg_model))
        self.config.video_feed_fps = int(request.args.get('video_feed_fps', self.config.video_feed_fps))
        self.config.send_mqtt = bool(request.args.get('send_mqtt', self.config.send_mqtt))
        self.config.send_webhook = bool(request.args.get('send_webhook', self.config.send_webhook))
        self.config.fps_print_frames = int(request.args.get('fps_print_frames', self.config.fps_print_frames))
        self.config.mqtt_heartbeat_secs = int(request.args.get('mqtt_heartbeat_secs', self.config.mqtt_heartbeat_secs))
        self.config.presence_cooldown_secs = int(
            request.args.get('presence_cooldown_secs', self.config.presence_cooldown_secs))
        self.config.presence_warmup_secs = int(
            request.args.get('presence_warmup_secs', self.config.presence_warmup_secs))
        self.config.argos_person_detection_enabled = int(
            request.args.get('argos_person_detection_enabled', self.config.argos_person_detection_enabled))
        self.config.argos_detection_threshold = float(
            request.args.get('argos_detection_threshold', self.config.argos_detection_threshold))
        self.config.argos_detection_frequency_frames = int(
            request.args.get('argos_detection_frequency_frames', self.config.argos_detection_frequency_frames))

        return jsonify(self.config.__dict__)

    @route('/camconfig')
    def camconfig(self):
        if self.config.input_mode == InputMode.PI_CAM:
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
