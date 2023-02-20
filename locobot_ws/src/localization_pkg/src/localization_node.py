#!/usr/bin/env python3

"""
Node to handle ROS interface for getting observations, running the localization filter, and publishing the best estimate of vehicle position on the map.
"""

import rospy
from sensor_msgs.msg import Image
from geometry_msgs.msg import Vector3
import rospkg, yaml
import numpy as np
import cv2
from cv_bridge import CvBridge

############ GLOBAL VARIABLES ###################
bridge = CvBridge()
localization_pub = None
pf = None
#################################################


def read_params():
    """
    Read configuration params from the yaml.
    """
    global cfg_debug_mode, topic_observations, topic_occ_map, topic_localization
    # Determine filepath.
    rospack = rospkg.RosPack()
    pkg_path = rospack.get_path('perception_pkg')
    # Open the yaml and get the relevant params.
    with open(pkg_path+'/config/config.yaml', 'r') as file:
        config = yaml.safe_load(file)
        cfg_debug_mode = config["test"]["run_debug_mode"]
        # Rostopics:
        topic_observations = config["topics"]["observations"]
        topic_occ_map = config["topics"]["occ_map"]
        topic_localization = config["topics"]["localization"]


def get_observation(msg):
    """
    Get an observation Image from the ML model's output.
    """
    # TODO pf.update_with_observation(msg.data)
    # TODO Get the best particle estimate from the filter, and publish it.


def get_occ_map(msg):
    """
    Get the processed occupancy grid map to use for PF measurement likelihood.
    """
    # Convert from ROS Image message to an OpenCV image.
    occ_map = bridge.imgmsg_to_cv2(msg, desired_encoding='passthrough')
    # TODO pf.set_map(occ_map)


def main():
    global localization_pub
    rospy.init_node('localization_node')

    read_params()
    # Init the particle filter instance.

    # Subscribe to occupancy grid map. Needed for PF's measurement likelihood step.
    rospy.Subscriber(topic_occ_map, Image, get_occ_map, queue_size=1)
    # Subscribe to observations.
    rospy.Subscriber(topic_observations, Image, get_observation, queue_size=1)
    # TODO Subscribe to commands or odometry. Needed to propagate particles between iterations.

    # Publish localization estimate.
    localization_pub = rospy.Publisher(topic_localization, Vector3, queue_size=1)

    rospy.spin()


if __name__ == '__main__':
    try:
        main()
    except rospy.ROSInterruptException:
        pass