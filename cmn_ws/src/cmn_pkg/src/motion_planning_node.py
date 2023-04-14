#!/usr/bin/env python3

"""
Node to handle ROS interface for getting localization estimate, global map, and performing path planning, navigation, and publishing a control command to the turtlebot.
"""

import rospy, sys
from geometry_msgs.msg import Twist, Vector3
from sensor_msgs.msg import Image
from std_msgs.msg import Float32MultiArray
import rospkg, yaml
import numpy as np
import cv2
from cv_bridge import CvBridge
from random import random
from math import pi

from scripts.cmn_utilities import clamp, ObservationGenerator
from scripts.astar import Astar
from scripts.pure_pursuit import PurePursuit

############ GLOBAL VARIABLES ###################
bridge = CvBridge()
obs_gen = ObservationGenerator()
astar = Astar()
# goal pose in global map coords (row, col).
goal_pos_px = None
#################################################

##################### UTILITY FUNCTIONS #######################
def read_params():
    """
    Read configuration params from the yaml.
    """
    # Determine filepath.
    rospack = rospkg.RosPack()
    pkg_path = rospack.get_path('cmn_pkg')
    # Open the yaml and get the relevant params.
    with open(pkg_path+'/config/config.yaml', 'r') as file:
        config = yaml.safe_load(file)
        global g_debug_mode, g_do_path_planning
        g_debug_mode = config["test"]["run_debug_mode"]
        g_do_path_planning = config["path_planning"]["do_path_planning"]
        PurePursuit.use_finite_lookahead_dist = g_do_path_planning
        # Rostopics.
        global g_topic_commands, g_topic_localization, g_topic_occ_map, g_topic_planned_path, g_topic_goal
        g_topic_occ_map = config["topics"]["occ_map"]
        g_topic_localization = config["topics"]["localization"]
        g_topic_goal = config["topics"]["goal"]
        g_topic_commands = config["topics"]["commands"]
        g_topic_planned_path = config["topics"]["planned_path"]
        # Constraints.
        global g_max_fwd_cmd, g_max_ang_cmd
        g_max_fwd_cmd = config["constraints"]["fwd"]
        g_max_ang_cmd = config["constraints"]["ang"]
        # In motion test mode, only this node will run, so it will handle the timer.
        global g_dt
        g_dt = config["dt"]

def publish_command(fwd, ang):
    """
    Clamp a command within valid values, and publish it to the vehicle/simulator.
    """
    # Clamp to allowed velocity ranges.
    fwd = clamp(fwd, 0, g_max_fwd_cmd)
    ang = clamp(ang, -g_max_ang_cmd, g_max_ang_cmd)
    rospy.loginfo("MOT: Publishing a command ({:}, {:})".format(fwd, ang))
    # Create ROS message.
    msg = Twist(Vector3(fwd, 0, 0), Vector3(0, 0, ang))
    cmd_pub.publish(msg)

######################## CALLBACKS ########################
def test_timer_callback(event):
    """
    Only runs in test mode.
    Publish desired type of test motion every iteration.
    """
    fwd, ang = 0.0, 0.0
    if g_test_motion_type == "none":
        pass
    elif g_test_motion_type == "circle":
        fwd, ang = g_max_fwd_cmd, g_max_ang_cmd
    elif g_test_motion_type == "straight":
        fwd, ang = g_max_fwd_cmd, 0.0
    elif g_test_motion_type == "random":
        rospy.logerr("MOT: test random motion not yet implemented.")
    else:
        rospy.logerr("MOT: test mode called with invalid test_motion_type: {:}".format(g_test_motion_type))
        exit()
    # Send the motion to the robot.
    publish_command(fwd, ang)

def get_localization_est(msg:Vector3):
    """
    Get localization estimate from the particle filter.
    """
    # TODO process it and associate with a particular cell/orientation on the map.
    rospy.loginfo("MOT: Got localization estimate ({:.2f}, {:.2f}, {:.2f})".format(msg.x, msg.y, msg.z))
    # Convert message into numpy array (x,y,yaw).
    pose_est = np.array([msg.x, msg.y, msg.z])

    # Choose a motion command to send. Must send something to keep cycle going.
    fwd, ang = 0.0, 0.0
    if goal_pos_px is None:
        rospy.loginfo("MOT: No goal point, so commanding constant motion.")
        # Set a simple motion command, since we have no goal to plan towards.  
        # fwd, ang = 0.0, 0.0 # do nothing.
        fwd, ang = g_max_fwd_cmd, g_max_ang_cmd # drive in a small circle, limited by motion constraints.
        # fwd, ang = g_max_fwd_cmd, 0.0 # drive in a straight line.
    else:
        rospy.loginfo("MOT: Goal point exists, so planning a path there.")
        # Plan a path from this estimated position to the goal.
        fwd, ang = plan_path_to_goal(pose_est)
    # Publish the motion command.
    publish_command(fwd, ang)

def get_goal_pos(msg:Vector3):
    """
    Get goal position in pixels.
    For now, this is obtained from the user clicking on the map in the sim viz.
    """
    rospy.loginfo("MOT: Got goal pos ({:}, {:})".format(int(msg.x), int(msg.y)))
    global goal_pos_px
    goal_pos_px = (int(msg.x), int(msg.y))
    
def get_map(msg:Image):
    """
    Get the global occupancy map to use for path planning.
    NOTE Map was already processed into an occupancy grid before being sent.
    """
    rospy.loginfo("MOT: Got occupancy map.")
    # Convert from ROS Image message to an OpenCV image.
    occ_map = bridge.imgmsg_to_cv2(msg, desired_encoding='passthrough')
    obs_gen.set_map(occ_map)
    astar.map = obs_gen.map

################ PATH PLANNING FUNCTIONS #####################
def plan_path_to_goal(veh_pose_est):
    """
    Given a desired goal point, use A* to generate a path there,
    starting at the current localization estimate.
    @param veh_pose_est, 3x1 numpy array of localization estimate (x,y,yaw) in meters.
    """
    # Convert vehicle pose from meters to pixels.
    veh_r, veh_c = obs_gen.transform_map_m_to_px(veh_pose_est[0], veh_pose_est[1])

    if g_do_path_planning:
        # Generate (reverse) path with A*.
        path_px_rev = astar.run_astar(veh_r, veh_c, goal_pos_px[0], goal_pos_px[1])
    else:
        # Just use the goal point as the "path".
        path_px_rev = [goal_pos_px, (veh_r, veh_c)]
    if path_px_rev is None:
        rospy.logerr("MOT: No path found by A*. Publishing zeros for motion command.")
        return 0.0, 0.0
    # rospy.loginfo("MOT: Planned path from A*: " + str(path_px_rev))
    # Turn this path from px to meters and reverse it.
    path = []
    for i in range(len(path_px_rev)-1, -1, -1):
        path.append(obs_gen.transform_map_px_to_m(path_px_rev[i][0], path_px_rev[i][1]))
        # Check if the path contains any occluded cells.
        if obs_gen.map[path_px_rev[i][0], path_px_rev[i][1]] == 0:
            rospy.logwarn("MOT: Path contains an occluded cell.")

    # Set the path for pure pursuit, and generate a command.
    PurePursuit.path_meters = path
    fwd, ang = PurePursuit.compute_command(veh_pose_est)
    # Keep within constraints.
    fwd_clamped = clamp(fwd, 0, g_max_fwd_cmd)
    ang_clamped = clamp(ang, -g_max_ang_cmd, g_max_ang_cmd)
    if fwd != fwd_clamped or ang != ang_clamped:
        rospy.logwarn("MOT: Clamped pure pursuit output from ({:.2f}, {:.2f}) to ({:.2f}, {:.2f}).".format(fwd, ang, fwd_clamped, ang_clamped))

    # Publish the path in pixels for the plotter to display.
    path_as_list = [path_px_rev[i][0] for i in range(len(path_px_rev))] + [path_px_rev[i][1] for i in range(len(path_px_rev))]
    path_pub.publish(Float32MultiArray(data=path_as_list))

    # Return the motion command to be published.
    return fwd_clamped, ang_clamped


# TODO obstacle avoidance?


def main():
    global cmd_pub, path_pub
    rospy.init_node('motion_planning_node')

    read_params()

    # Read command line args.
    if len(sys.argv) > 1:
        rospy.logwarn("MOT: Running motion_planning_node in test mode, on its own timer.")
        global g_run_test_motion, g_test_motion_type
        g_run_test_motion = sys.argv[1].lower() == "true"
        g_test_motion_type = sys.argv[2]

    # Subscribe to localization est.
    rospy.Subscriber(g_topic_localization, Vector3, get_localization_est, queue_size=1)
    # Subscribe to goal position in pixels on the map.
    rospy.Subscriber(g_topic_goal, Vector3, get_goal_pos, queue_size=1)
    # Subscribe to (or just read the map from) file.
    rospy.Subscriber(g_topic_occ_map, Image, get_map, queue_size=1)

    # Publish control commands (velocities in m/s and rad/s).
    cmd_pub = rospy.Publisher(g_topic_commands, Twist, queue_size=1)
    # there is a way to command a relative position/yaw motion:
    # python navigation/base_position_control.py --base_planner none --base_controller ilqr --smooth --close_loop --relative_position 1.,1.,1.57 --botname locobot

    # Publish planned path to the goal (for viz).
    path_pub = rospy.Publisher(g_topic_planned_path, Float32MultiArray, queue_size=1)

    # In test mode, start a timer to publish commands to the robot.
    if g_run_test_motion:
        rospy.Timer(rospy.Duration(g_dt), test_timer_callback)

    rospy.spin()


if __name__ == '__main__':
    try:
        main()
    except rospy.ROSInterruptException:
        pass