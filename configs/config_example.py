class Config:
    def __init__(self):
        # whether to show fps in the output video
        self.show_fps = True

        # prints the current motion detection fps at this frequency of frames
        self.fps_print_frames = 10000

        # initial number of frames on startup, to consider for making the "backgroud"
        # for further motion detection
        self.md_warmup_frame_count = 100

        # whether to show all smaller motion contours of motion detection
        self.md_show_all_contours = True

        # whether to merge the foreground frames into the background while capturing
        # disabling this allow you to consider any objects which appear after the initial
        # warmup frames to be "motion"
        # enabling this (default) would keep merging the foreground into the background
        # and consider only changes to be "motion" - this is more realistic as furniture
        # can be moved, you can keep a phone or other items in the scene, your lighting
        # can change and the motion detector won't keep considering them as motion forever
        self.md_update_bg_model = True

        # to reset the background model - useful to do this as an automation if
        # you have md_update_bg_model set to False for some reason
        self.md_reset_bg_model = False

        # the image thresholding value for the motion detector
        # read about image thresholding in opencv here: https://docs.opencv.org/master/d7/d4d/tutorial_py_thresholding.html
        self.md_tval = 25

        # the small box of change to consider as motion
        self.md_min_cont_area = 0

        # enable erode (https://docs.opencv.org/3.4/db/df6/tutorial_erosion_dilatation.html)
        self.md_enable_erode = False

        # enable dilate (https://docs.opencv.org/3.4/db/df6/tutorial_erosion_dilatation.html)
        self.md_enable_dilate = True
        self.md_erode_iterations = 2
        self.md_dilate_iterations = 2

        # the higher the background accumulation weight the lower the "memory" of the motion detector
        # for new objects. in other words a higher value will show motion detected for shorter periods
        # while lower value will show motion detected longer for any new objects that came into the frame
        # tune this as per the fps of your video stream and speed of objects you are detecting (e.g. a snail vs a person)
        self.md_bg_accum_weight = 0.1

        # do motion detection only within this mask
        self.md_mask = None

        # don't do motion detection in this mask
        self.md_nmask = None

        # minimum size of the box for detected motion (useful for filtering small motion like tiny shadows or curtains moving)
        self.md_box_threshold_x = 0
        self.md_box_threshold_y = 0

        # if enabled will write the frame which caused presence to go ON to a file
        self.md_first_frame_write = True

        # folder where motion frames should be saved. they are saved as timestamped jpegs
        self.md_first_frame_write_path = "/home/pi/motion_frames"

        # the fps to limit the output video feed to.
        # not necessary since it is automatically limited to the speed of the motion detector
        self.video_feed_fps = 5

        # whether to enable MQTT notifications to HA
        self.send_mqtt = True

        # usual mqtt stuff to connect to HA
        self.mqtt_host = ''
        self.mqtt_port = 1883
        self.mqtt_username = ""
        self.mqtt_password = ""
        # keep sending the last motion state every x seconds (in case HA restarted or just didnt
        # get our message last time
        self.mqtt_heartbeat_secs = 10

        # topic where presence state changes are sent
        self.mqtt_state_topic = 'home-assistant/picam-object-presence/sensor1'

        # the number of seconds for the coolDown period
        # coolDown period: When motion tries to switch presenceStatus from on to off,
        # we have a coolDown period where the argos object detection service is called
        # to figure out if there's a person in the scene, and we keep extending the
        # cool down till a person is detected. This is to avoid false negatives
        # read more about how argos-presence works in the README
        self.presence_cooldown_secs = 300

        # the number of seconds for the warmUp period
        # When motion tries to switch presenceStatus from off to on, we have a warmUp
        # period where, again we detect if a person is present or not. This is to avoid
        # false positives. For example, if your presenceStatus recently went from on
        # to off, your lights are in the process of turning off, which can be seen as
        # motion by the detector. If we did not have a warmUp period, your room would
        # keep flipping the lights on and off continuously. Note: this doesmn't meant
        # you have to wait 30 seconds (warmUp seconds) for your lights to turn on.
        # During warmup, it terminates the warmup and switches to presenceStatus
        # ON immediately if a person is detected (and only if…). The warmUp period is
        # only in effect after a recent change from presence to no presence (from last motion).
        # The other times whenever you come into the room, argos will go into presence status
        # immediately (with just motion). You can even turn off warmUp mode by setting
        # warmUp seconds to 0.
        self.presence_warmup_secs = 30

        # whether to enable person detection by calling the argos object detection service
        # if off argos-presence will just rely on the motion detection algorithm for presence
        self.argos_person_detection_enabled = True

        # the argos object detection API url (could be running on the same or remote host)
        self.argos_service_api_url = 'http://<argos-host>:8080/detect'

        # the detection threshold to consider a person a person (from 0 to 1)
        # usually passed to tensorflow (if argos is configured to use tensorflow)
        self.argos_detection_threshold = 0.5

        # negative mask to apply to image for the object detector
        self.argos_detection_nmask = (190, 0, 260, 65)

        # this allows throttling the calls to the argos service
        # do person detections only at this frame frequency
        self.argos_detection_frequency_frames = 20

        # if you have privacy concerns about your presence camera video/image feed
        # then you can disable the output frame
        self.output_frame_enabled = True