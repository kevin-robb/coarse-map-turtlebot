#!/usr/bin/env python3

"""
Set of static functions to perform pure pursuit navigation.
"""

from math import remainder, tau, pi, atan2, sqrt
from time import time

class PurePursuit:
    # Pure pursuit params.
    lookahead_dist_init = 0.2 # meters.
    lookahead_dist_max = 2 # meters.
    # Path to follow.
    path_meters = []
    # PID vars.
    integ = 0
    err_prev = 0.0
    last_time = 0.0

    @staticmethod
    def compute_command(cur):
        """
        Determine odom command to stay on the path.
        """
        # pare the path up to current veh pos.
        PurePursuit.pare_path(cur)

        if len(PurePursuit.path_meters) < 1: 
            # if there's no path yet, just wait. (send 0 cmd)
            return 0.0, 0.0

        # define lookahead point.
        lookahead_pt = None
        lookahead_dist = PurePursuit.lookahead_dist_init # starting search radius.
        # look until we find the path, or give up at the maximum dist.
        while lookahead_pt is None and lookahead_dist <= PurePursuit.lookahead_dist_max: 
            lookahead_pt = PurePursuit.choose_lookahead_pt(cur, lookahead_dist)
            lookahead_dist *= 1.25
        # make sure we actually found the path.
        if lookahead_pt is None:
            # we can't see the path, so just try to go to the first pt.
            lookahead_pt = PurePursuit.path_meters[0]
        
        # compute global heading to lookahead_pt
        gb = atan2(lookahead_pt[1] - cur[1], lookahead_pt[0] - cur[0])
        # compute hdg relative to veh pose.
        beta = remainder(gb - cur[2], tau)

        # compute time since last iteration.
        dt = 0
        if PurePursuit.last_time != 0:
            dt = time() - PurePursuit.last_time
        PurePursuit.last_time = time()
            
        # Update global integral term.
        PurePursuit.integ += beta * dt

        # Update PID terms.
        P = 0.5 * beta # proportional to hdg error.
        I = 0.0 * PurePursuit.integ # integral to correct systematic error.
        D = 0.0 * (beta - PurePursuit.err_prev) / dt # slope to reduce oscillation.
        ang = P + I + D
        # Compute forward velocity control command using hdg error beta.
        fwd = 0.02 * (1 - abs(beta / pi))**12 + 0.01
        # Save err for next iteration.
        PurePursuit.err_prev = beta
        
        return fwd, ang


    @staticmethod
    def pare_path(cur):
        """
        If the vehicle is near a path pt, cut the path off up to this pt.
        """
        for i in range(len(PurePursuit.path_meters)):
            r = ((cur[0]-PurePursuit.path_meters[i][0])**2 + (cur[1]-PurePursuit.path_meters[i][1])**2)**(1/2)
            if r < 0.15:
                # remove whole path up to this pt.
                del PurePursuit.path_meters[0:i+1]
                return


    @staticmethod
    def choose_lookahead_pt(cur, lookahead_dist):
        """
        Find the point on the path at the specified radius from the current veh pos.
        """
        # if there's only one path point, go straight to it.
        if len(PurePursuit.path_meters) == 1:
            return PurePursuit.path_meters[0]
        lookahead_pt = None
        # check the line segments between each pair of path points.
        for i in range(1, len(PurePursuit.path_meters)):
            # get vector between path pts.
            diff = [PurePursuit.path_meters[i][0]-PurePursuit.path_meters[i-1][0], PurePursuit.path_meters[i][1]-PurePursuit.path_meters[i-1][1]]
            # get vector from veh to first path pt.
            v1 = [PurePursuit.path_meters[i-1][0]-cur[0], PurePursuit.path_meters[i-1][1]-cur[1]]
            # compute coefficients for quadratic eqn to solve.
            a = diff[0]**2 + diff[1]**2
            b = 2*(v1[0]*diff[0] + v1[1]*diff[1])
            c = v1[0]**2 + v1[1]**2 - lookahead_dist**2
            try:
                discr = sqrt(b**2 - 4*a*c)
            except:
                # discriminant is negative, so there are no real roots.
                # (line segment is too far away)
                continue
            # compute solutions to the quadratic.
            # these will tell us what point along the 'diff' line segment is a solution.
            q = [(-b-discr)/(2*a), (-b+discr)/(2*a)]
            # check validity of solutions.
            valid = [q[i] >= 0 and q[i] <= 1 for i in range(2)]
            # compute the intersection pt. it's the first seg pt + q percent along diff vector.
            if valid[0]: lookahead_pt = [PurePursuit.path_meters[i-1][0]+q[0]*diff[0], PurePursuit.path_meters[i-1][1]+q[0]*diff[1]]
            elif valid[1]: lookahead_pt = [PurePursuit.path_meters[i-1][0]+q[1]*diff[0], PurePursuit.path_meters[i-1][1]+q[1]*diff[1]]
            else: continue # no intersection pt in the allowable range.
        return lookahead_pt

