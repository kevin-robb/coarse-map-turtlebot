#!/usr/bin/env python3

"""
Main node for running the project. This should be run on the host PC while the locobot is connected.
"""

import rospy
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Image, LaserScan, PointCloud2
from nav_msgs.msg import Odometry
from std_msgs.msg import Empty, Bool
import rospkg, yaml, sys, os
from cv_bridge import CvBridge
from math import pi, atan2, asin
import numpy as np
import cv2
from time import strftime, time
from typing import Callable

from scripts.cmn_interface import CoarseMapNavInterface, CmnConfig
from scripts.basic_types import PoseMeters, PosePixels, rotate_image_to_north
import locobot_interface

############ GLOBAL VARIABLES ###################
g_cv_bridge = CvBridge()

g_cmn_interface:CoarseMapNavInterface = None

# RealSense measurements buffer.
g_most_recent_rgb_meas = None
g_desired_meas_shape = None # Shape (h, w, c) to resize each color image from RS to.
g_most_recent_depth_meas = None # Most recent raw rect depth image or pointcloud, depending on which is set to be used in config.
g_depth_proc_func:Callable = None # Function to process depth image or pointcloud, depending on setting.
# Odom measurements.
g_first_odom:PoseMeters = None # Used to offset odom frame to always have origin at start pose.
# Configs.
g_run_modes = ["continuous", "discrete", "discrete_random"] # Allowed/supported run modes.
g_run_mode = None # "discrete" or "continuous"
g_use_ground_truth_map_to_generate_observations = False
g_show_live_viz = False
g_verbose = False
g_use_lidar_as_ground_truth = False
g_manual_goal_cell:PosePixels = None # Goal cell can be specified in the configs. If this is left as None, a random free cell will be chosen.
# Data saving params.
g_save_training_data:bool = False # Flag to save data when running on robot for later training/evaluation.
g_training_data_dirpath:str = None # Location of directory to save data to.
# Live flags.
g_viz_paused = False

# Publish visualizer images so we can view them with rqt without disrupting the run loop.
g_pub_viz_images:bool = False
g_sim_viz_pub = None
g_cmn_viz_pub = None
#################################################

def timer_update_loop(event=None):
    # Update the visualization, if enabled.
    if g_cmn_interface.visualizer is not None:
        # Simulator viz.
        sim_viz_img = None
        if g_use_ground_truth_map_to_generate_observations:
            sim_viz_img = g_cmn_interface.visualizer.get_updated_img()
        # CMN viz.
        cmn_viz_img = None
        if g_cmn_interface.cmn_node is not None and g_cmn_interface.cmn_node.visualizer is not None:
            cmn_viz_img = g_cmn_interface.cmn_node.visualizer.get_updated_img()
            
        if g_pub_viz_images:
            # Publish to rostopics for some external viz to use.
            if sim_viz_img is not None:
                g_sim_viz_pub.publish(g_cv_bridge.cv2_to_imgmsg(sim_viz_img))
            if cmn_viz_img is not None:
                g_cmn_viz_pub.publish(g_cv_bridge.cv2_to_imgmsg(cmn_viz_img))
        else:
            # Manage the viz here.
            if sim_viz_img is not None:
                # cv2.namedWindow('viz image', cv2.WINDOW_NORMAL)
                cv2.imshow('viz image', sim_viz_img)
            if cmn_viz_img is not None:
                # cv2.namedWindow('cmn viz image', cv2.WINDOW_NORMAL)
                cv2.imshow('cmn viz image', cmn_viz_img)
            key = cv2.waitKey(int(g_dt * 1000))
            # Special keypress conditions.
            if key == 113: # q for quit.
                cv2.destroyAllWindows()
                rospy.signal_shutdown("User pressed Q key.")
                exit()
            elif key == 32: # spacebar.
                global g_viz_paused
                g_viz_paused = not g_viz_paused

            if g_viz_paused:
                # Skip all operations, so the same viz image will just keep being displayed until unpaused.
                return

    # Only gather a pano RGB if needed.
    pano_rgb = None
    local_occ_depth = None
    if g_cmn_interface.last_pano_rgb is not None:
        # The robot only turned in place, so the pano from last iteration was rotated and can now be used again.
        pano_rgb = g_cmn_interface.last_pano_rgb
        local_occ_depth = g_cmn_interface.last_depth_local_occ
    elif g_use_ground_truth_map_to_generate_observations:
        # Sim will be used inside CMN interface to generate local occ.
        pass
    elif g_use_lidar_as_ground_truth:
        # Use local occ from LiDAR.
        pass
    else:
        # Pano RGB will be used to predict local occ.
        # This will generate ground truth from depth data too if g_use_depth_as_ground_truth.
        pano_rgb, local_occ_depth = get_pano_meas()

    # Get LiDAR local occ meas for comparison.
    if not g_use_ground_truth_map_to_generate_observations and locobot_interface.g_lidar_local_occ_meas is not None:
        # Update the viz, unless we're running the sim, since it would be confusing to show unused LiDAR occ grid in the viz.
        g_cmn_interface.cmn_node.visualizer.lidar_local_occ_meas = locobot_interface.g_lidar_local_occ_meas
        
    # Run an iteration. (It will internally run either continuous or discrete case).
    g_cmn_interface.run(pano_rgb, g_dt, locobot_interface.g_lidar_local_occ_meas, local_occ_depth)


##################### UTILITY FUNCTIONS #######################
def read_params():
    """
    Read configuration params from the yaml.
    """
    # Determine filepath.
    rospack = rospkg.RosPack()
    pkg_path = rospack.get_path('cmn_pkg')
    global g_yaml_path
    g_yaml_path = os.path.join(pkg_path, 'config/config.yaml')
    # Open the yaml and get the relevant params.
    with open(g_yaml_path, 'r') as file:
        config = yaml.safe_load(file)
        global g_verbose, g_dt, g_enable_localization, g_enable_ml_model, g_discrete_assume_yaw_is_known
        g_verbose = config["verbose"]
        g_dt = config["dt"]
        g_enable_localization = config["particle_filter"]["enable"]
        g_enable_ml_model = not config["model"]["skip_loading"]
        g_discrete_assume_yaw_is_known = config["discrete_assume_yaw_is_known"]
        # Goal cell params.
        if config["manually_set_goal_cell"]:
            global g_manual_goal_cell
            g_manual_goal_cell = PosePixels(config["goal_row"], config["goal_col"])
        # LiDAR params.
        global g_use_lidar_as_ground_truth, g_fuse_lidar_with_rgb, g_use_depth_as_ground_truth
        g_use_lidar_as_ground_truth = config["lidar"]["use_lidar_as_ground_truth"]
        g_fuse_lidar_with_rgb = config["lidar"]["fuse_lidar_with_rgb"]
        g_use_depth_as_ground_truth = config["depth"]["use_depth_as_ground_truth"]
        if g_use_depth_as_ground_truth:
            # Choose function to process depth data. This depends on whether we're using raw rect depth images, or the processed pointclouds.
            global g_use_depth_pointcloud, g_depth_proc_func
            g_use_depth_pointcloud = config["depth"]["use_pointcloud"]
            if g_use_depth_pointcloud:
                g_depth_proc_func = locobot_interface.get_local_occ_from_pointcloud
            else:
                g_depth_proc_func = locobot_interface.get_local_occ_from_depth
            
        locobot_interface.read_params()
        # Settings for interfacing with CMN.
        global g_meas_topic, g_desired_meas_shape
        g_meas_topic = config["measurements"]["topic"]
        g_desired_meas_shape = (config["measurements"]["height"], config["measurements"]["width"])
        # Settings for saving data for later training/evaluation.
        global g_save_training_data, g_training_data_dirpath
        g_save_training_data = config["save_data_for_training"]
        if g_save_training_data:
            g_training_data_dirpath = config["training_data_dirpath"]
            if g_training_data_dirpath[0] != "/":
                # Make path relative to cmn_pkg directory.
                g_training_data_dirpath = os.path.join(pkg_path, g_training_data_dirpath)
            # Append datetime and create data directory.
            g_training_data_dirpath = os.path.join(g_training_data_dirpath, strftime("%Y%m%d-%H%M%S"))
            os.makedirs(g_training_data_dirpath, exist_ok=True)


def set_global_params(run_mode:str, use_sim:bool, use_viz:bool, cmd_vel_pub=None):
    """
    Set global params specified by the launch file/runner.
    @param run_mode - Mode to run the project in.
    @param use_sim - Flag to use the simulator instead of requiring robot sensor data.
    @param use_viz - Flag to show the live visualization. Only possible on host PC.
    @param cmd_vel_pub (optional) - ROS publisher for command velocities.
    """
    # Set the global params.
    global g_run_mode, g_use_ground_truth_map_to_generate_observations, g_show_live_viz
    g_run_mode = run_mode
    g_use_ground_truth_map_to_generate_observations = use_sim
    g_show_live_viz = use_viz

    # Setup configs for CMN interface.
    config = CmnConfig()
    config.run_mode = run_mode
    config.enable_sim = use_sim
    config.enable_viz = use_viz
    config.enable_ml_model = g_enable_ml_model
    config.enable_localization = g_enable_localization
    config.use_lidar_as_ground_truth = g_use_lidar_as_ground_truth and not use_sim
    config.fuse_lidar_with_rgb = g_fuse_lidar_with_rgb and not g_use_lidar_as_ground_truth and not use_sim and g_enable_ml_model
    config.use_depth_as_ground_truth = g_use_depth_as_ground_truth and not g_use_lidar_as_ground_truth and not use_sim
    config.assume_yaw_is_known = g_discrete_assume_yaw_is_known and "discrete" in g_run_mode
    if g_manual_goal_cell is not None:
        config.manually_set_goal_cell = True
        config.manual_goal_cell = g_manual_goal_cell

    # Init the main (non-ROS-specific) part of the project.
    global g_cmn_interface
    g_cmn_interface = CoarseMapNavInterface(config, cmd_vel_pub)

    # Set data saving params.
    g_cmn_interface.save_training_data = g_save_training_data
    g_cmn_interface.training_data_dirpath = g_training_data_dirpath


######################## CALLBACKS ########################
def get_pano_meas():
    """
    Get a panoramic measurement of RGB data, and depth if it's enabled.
    Since the robot has only a forward-facing camera, we must pivot in-place four times.
    @return panoramic image created by concatenating four individual measurements.
    @return local occ created from depth data, if it's enabled. None if disabled.
    """
    rospy.loginfo("Attempting to generate a panoramic measurement by commanding four 90 degree pivots.")
    local_occ_meas = None # Local occ from depth will remain None if disabled.

    # Get current RGB view.
    pano_meas_front = pop_from_RGB_buffer()
    if g_use_depth_as_ground_truth:
        # Generate local occ from current depth view.
        local_occ_east = g_depth_proc_func(pop_from_depth_buffer())

    # Pivot in-place 90 deg CW to get another measurement.
    g_cmn_interface.motion_planner.cmd_discrete_action("turn_right")
    pano_meas_right = pop_from_RGB_buffer()
    if g_use_depth_as_ground_truth:
        local_occ_south = g_depth_proc_func(pop_from_depth_buffer())

    g_cmn_interface.motion_planner.cmd_discrete_action("turn_right")
    pano_meas_back = pop_from_RGB_buffer()
    if g_use_depth_as_ground_truth:
        local_occ_west = g_depth_proc_func(pop_from_depth_buffer())

    g_cmn_interface.motion_planner.cmd_discrete_action("turn_right")
    pano_meas_left = pop_from_RGB_buffer()
    if g_use_depth_as_ground_truth:
        local_occ_north = g_depth_proc_func(pop_from_depth_buffer())

    g_cmn_interface.motion_planner.cmd_discrete_action("turn_right")
    # Vehicle should now be facing forwards again (its original direction).

    # Combine the RGB images into a panorama.
    pano_rgb = np.concatenate([pano_meas_front[:, :, 0:3],
                               pano_meas_right[:, :, 0:3],
                               pano_meas_back[:, :, 0:3],
                               pano_meas_left[:, :, 0:3]], axis=1)
    # Convert from RGB to BGR so OpenCV will show/save it properly.
    # TODO determine if this should be done for the model input or not.
    pano_rgb = cv2.cvtColor(pano_rgb, cv2.COLOR_RGB2BGR)

    if g_use_depth_as_ground_truth:
        # Fake the robot angle so we can use the existing function and get it to rotate how we need.
        rotated_local_occ_south = rotate_image_to_north(local_occ_south, 0)
        rotated_local_occ_west = rotate_image_to_north(local_occ_west, -np.pi/2)
        rotated_local_occ_north = rotate_image_to_north(local_occ_north, np.pi)

        # Combine these four partial local occupancy maps. Use min so occupied cells take priority.
        local_occ_meas = np.min([local_occ_east, rotated_local_occ_south, rotated_local_occ_west, rotated_local_occ_north], axis=0)

        # Check if the corner gaps should be occupied, despite being outside FOV.
        div = 3 # Number of groups to divide each side into.
        one_third = local_occ_meas.shape[0]//div
        two_thirds = (div-1)*local_occ_meas.shape[0]//div
        occ_thresh = 0.1 # threshold proportion of cells in this region that are occupied.
        top_left_block = local_occ_meas[:one_third, :one_third]
        top_left_occ_percent = 1 - np.mean(top_left_block)
        if top_left_occ_percent >= occ_thresh:
            local_occ_meas[:one_third, :one_third] = 0

        top_right_block = local_occ_meas[:one_third, two_thirds:]
        top_right_occ_percent = 1 - np.mean(top_right_block)
        if top_right_occ_percent >= occ_thresh:
            local_occ_meas[:one_third, two_thirds:] = 0

        bot_right_block = local_occ_meas[two_thirds:, two_thirds:]
        bot_right_occ_percent = 1 - np.mean(bot_right_block)
        if bot_right_occ_percent >= occ_thresh:
            local_occ_meas[two_thirds:, two_thirds:] = 0

        bot_left_block = local_occ_meas[two_thirds:, :one_third]
        bot_left_occ_percent = 1 - np.mean(bot_left_block)
        if bot_left_occ_percent >= occ_thresh:
            local_occ_meas[two_thirds:, :one_third] = 0

        # print("occ percents are {:.3f}, {:.3f}, {:.3f}, {:.3f}".format(top_left_occ_percent, top_right_occ_percent, bot_right_occ_percent, bot_left_occ_percent))

    return pano_rgb, local_occ_meas

def pop_from_RGB_buffer():
    """
    Wait for a new RealSense measurement to be available, and return it.
    """
    global g_most_recent_rgb_meas
    while g_most_recent_rgb_meas is None:
        rospy.logwarn("Waiting on RGB measurement from RealSense!")
        rospy.sleep(0.01)
    # Convert from ROS Image message to an OpenCV image.
    cv_img_meas = g_cv_bridge.imgmsg_to_cv2(g_most_recent_rgb_meas, desired_encoding='passthrough')
    # Ensure this same measurement will not be used again.
    g_most_recent_rgb_meas = None

    # Resize the image to the size expected by CMN.
    if g_verbose:
        # cv2.imshow("color image", cv_img_meas); cv2.waitKey(0); cv2.destroyAllWindows()
        rospy.loginfo("Trying to resize image from shape {:} to {:}".format(cv_img_meas.shape, g_desired_meas_shape))
    cv_img_meas = cv2.resize(cv_img_meas, g_desired_meas_shape)

    return cv_img_meas

def get_RGB_image(msg:Image):
    """
    Get a measurement Image from the RealSense camera.
    Could be changed multiple times before we need a measurement, so this allows skipping measurements to prefer recency.
    """
    global g_most_recent_rgb_meas
    g_most_recent_rgb_meas = msg

def get_odom(msg:Odometry):
    """
    Get an odometry message from the robot's mobile base.
    Parse the message to extract the desired position and orientation information.
    """
    # Extract x,y position.
    x = msg.pose.pose.position.x
    y = msg.pose.pose.position.y
    # Extract orientation from quaternion.
    # NOTE our "yaw" is the "roll" from https://stackoverflow.com/a/18115837/14783583
    q = msg.pose.pose.orientation
    # yaw = atan2(2.0*(q.y*q.z + q.w*q.x), q.w*q.w - q.x*q.x - q.y*q.y + q.z*q.z)
    # pitch = asin(-2.0*(q.x*q.z - q.w*q.y))
    roll = atan2(2.0*(q.x*q.y + q.w*q.z), q.w*q.w + q.x*q.x - q.y*q.y - q.z*q.z)
    # Create a pose object.
    odom_pose = PoseMeters(x, y, roll)

    # Since resetting the locobot's odom doesn't seem to work, just save the first odom and use it to offset all future measurements.
    global g_first_odom
    if g_first_odom is None:
        # This is the first measurement received. Use it as the origin for all future measurements.
        g_first_odom = odom_pose
    else:
        odom_pose.make_relative(g_first_odom)

    # Set this odom in our code.
    g_cmn_interface.set_new_odom(odom_pose)
    
    if g_verbose:
        rospy.loginfo("Got odom: {:}".format(odom_pose))

def get_lidar(msg:LaserScan):
    """
    Get a new LiDAR measurement, process it, and update our nodes as necessary.
    """
    locobot_interface.get_local_occ_from_lidar(msg)
    # Update the motion planner instantaneously so we can stop before hitting a wall.
    g_cmn_interface.motion_planner.obstacle_in_front_of_robot = locobot_interface.g_lidar_detects_robot_facing_wall

def get_RS_depth_image(msg:Image):
    """
    Get a depth image from the RealSense camera, and save it to be used when needed.
    """
    global g_most_recent_depth_meas
    g_most_recent_depth_meas = msg

def get_pointcloud_msg(msg:PointCloud2):
    """
    Get a pointcloud message, and save it to be used when needed.
    """
    global g_most_recent_depth_meas
    g_most_recent_depth_meas = msg

def pop_from_depth_buffer():
    """
    Wait for a new depth measurement to be available, and return it.
    This could be a raw rect depth image or a pointcloud, depending on settings.
    """
    global g_most_recent_depth_meas
    # Since the depth data takes some time to come in, blank it to None so we make sure the measurement has come in AFTER the pivot has finished.
    g_most_recent_depth_meas = None
    while g_most_recent_depth_meas is None:
        rospy.logwarn("Waiting on depth measurement from RealSense!")
        rospy.sleep(0.5)
    return g_most_recent_depth_meas

# def get_local_occ_from_depth():
#     """
#     Use depth measurements from realsense to build a local occupancy map.
#     Since the robot has only a forward-facing depth camera, we must pivot in-place four times.
#     @return local occupancy map.
#     """
#     rospy.loginfo("Attempting to generate a local occupancy measurement from depth data by commanding four 90 degree pivots.")
#     # Generate local occ from current depth view.
#     local_occ_east = g_depth_proc_func(pop_from_depth_buffer())
#     # Pivot in-place 90 deg CW to get another measurement.
#     g_cmn_interface.motion_planner.cmd_discrete_action("turn_right")
#     local_occ_south = g_depth_proc_func(pop_from_depth_buffer())
#     g_cmn_interface.motion_planner.cmd_discrete_action("turn_right")
#     local_occ_west = g_depth_proc_func(pop_from_depth_buffer())
#     g_cmn_interface.motion_planner.cmd_discrete_action("turn_right")
#     local_occ_north = g_depth_proc_func(pop_from_depth_buffer())
#     g_cmn_interface.motion_planner.cmd_discrete_action("turn_right")
#     # Vehicle should now be facing forwards again (its original direction).

#     # Fake the robot angle so we can use the existing function and get it to rotate how we need.
#     rotated_local_occ_south = rotate_image_to_north(local_occ_south, 0)
#     rotated_local_occ_west = rotate_image_to_north(local_occ_west, -np.pi/2)
#     rotated_local_occ_north = rotate_image_to_north(local_occ_north, np.pi)

#     # Combine these four partial local occupancy maps. Use min so occupied cells take priority.
#     local_occ_meas = np.min([local_occ_east, rotated_local_occ_south, rotated_local_occ_west, rotated_local_occ_north], axis=0)

#     cv2.imshow('local_occ_east', local_occ_east)
#     cv2.imshow('rotated_local_occ_south', rotated_local_occ_south)
#     cv2.imshow('rotated_local_occ_west', rotated_local_occ_west)
#     cv2.imshow('rotated_local_occ_north', rotated_local_occ_north)
#     cv2.imshow('local_occ_meas', local_occ_meas)
#     cv2.waitKey(0)

#     return local_occ_meas


def main():
    rospy.init_node('runner_node')

    read_params()

    # # Reset the robot odometry. NOTE this doesn't seem to work.
    # odom_reset_pub = rospy.Publisher("/locobot/mobile_base/commands/reset_odometry", Empty, queue_size=10)
    # # NOTE: odom reset messages take some time to actually get through, so keep publishing for a duration.
    # odom_reset_pub_duration = 0.25 # seconds.
    # timer = time()
    # while time() - timer < odom_reset_pub_duration:
    #     odom_reset_pub.publish(Empty())

    # Publish control commands (velocities in m/s and rad/s).
    cmd_vel_pub = rospy.Publisher("/locobot/mobile_base/commands/velocity", Twist, queue_size=1)

    # Get any params specified in args from launch file.
    if len(sys.argv) > 3:
        set_global_params(sys.argv[1], sys.argv[2].lower() == "true", sys.argv[3].lower() == "true", cmd_vel_pub)
    else:
        print("Missing required arguments.")
        exit()

    if g_run_mode not in g_run_modes:
        rospy.logerr("Invalid run_mode {:}. Exiting.".format(g_run_mode))
        exit()


    # Subscribe to sensor images from RealSense.
    # TODO may want to check /locobot/camera/color/camera_info
    rospy.Subscriber(g_meas_topic, Image, get_RGB_image, queue_size=1)

    # Subscribe to robot odometry.
    rospy.Subscriber("/locobot/mobile_base/odom", Odometry, get_odom, queue_size=1)

    # Subscribe to LiDAR data, which will be used to avoid running into things, and may be used in place of local occ predictions.
    rospy.Subscriber("/locobot/scan", LaserScan, get_lidar, queue_size=1)

    # Only subscribe to the depth feed we'll be using.
    if g_use_depth_pointcloud:
        # Subscribe to depth cloud processed from depth image.
        rospy.Subscriber("/locobot/camera/depth/points", PointCloud2, get_pointcloud_msg, queue_size=1)
    else:
        # Subscribe to depth data from RealSense.
        rospy.Subscriber("/locobot/camera/depth/image_rect_raw", Image, get_RS_depth_image, queue_size=1)


    # Publish viz images so we can view them in rqt without messing up the run loop.
    global g_sim_viz_pub, g_cmn_viz_pub
    g_sim_viz_pub = rospy.Publisher("/cmn/viz/sim", Image, queue_size=1)
    g_cmn_viz_pub = rospy.Publisher("/cmn/viz/cmn", Image, queue_size=1)

    rospy.Timer(rospy.Duration(g_dt), timer_update_loop)

    rospy.spin()


if __name__ == '__main__':
    try:
        main()
    except rospy.ROSInterruptException:
        pass