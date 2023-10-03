#!/usr/bin/env python3

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpec
import cv2
import numpy as np
from typing import Tuple

from skimage.transform import resize, rotate
from skimage.color import rgb2gray


class CoarseMapNavVisualizer:
    """
    Functions to visualize discrete CMN data during the run.
    """
    # Sensor data & observations.
    pano_rgb = None # Current panoramic RGB measurement that was used to generate the current local map.
    current_predicted_local_map = None # Current observation generated by the ML model.
    current_ground_truth_local_map = None # Current observation generated by the simulator (if enabled).
    # Beliefs.
    predictive_belief_map = None # Prediction update step.
    observation_prob_map = None # Measurement update step.
    agent_belief_map = None # Combined updated belief.


    def __init__(self):
        """
        Initialize the CMN Visualizer.
        """
        pass

    @staticmethod
    def normalize_belief_for_visualization(belief):
        """
        @param belief - 2D numpy array of floats.
        """
        v_min = belief.min()
        v_max = belief.max()
        belief = (belief - v_min) / (v_max - v_min + 1e-8)
        return np.clip(belief, a_min=0, a_max=1)


    def get_updated_img(self):
        """
        Update the plot with all the most recent data, and redraw the viz.
        @ref https://stackoverflow.com/a/62040123/14783583
        @return new viz image as a cv/numpy matrix.
        """
        # Make a Figure and attach it to a canvas.
        fig = Figure(figsize=(8, 6), dpi=100)
        canvas = FigureCanvasAgg(fig)
        grid = GridSpec(3, 3, figure=fig)

        # Add subplots for observations and local occupancy GT / Pred
        ax_pano_rgb = fig.add_subplot(grid[0, :])
        ax_local_occ_gt = fig.add_subplot(grid[1, 0])
        ax_local_occ_pred = fig.add_subplot(grid[1, 1])
        ax_top_down_view = fig.add_subplot(grid[1, 2])
        # Add subplots for beliefs
        ax_pred_update_bel = fig.add_subplot(grid[2, 0])
        ax_obs_update_bel = fig.add_subplot(grid[2, 1])
        ax_belief = fig.add_subplot(grid[2, 2])

        # Set titles and remove axis
        ax_pano_rgb.set_title("Panoramic RGB observation")
        ax_pano_rgb.axis("off")
        ax_local_occ_gt.set_title("GT local occ")
        ax_local_occ_gt.axis("off")
        ax_local_occ_pred.set_title("Pred local occ")
        ax_local_occ_pred.axis("off")
        ax_top_down_view.set_title("Top down view")
        ax_top_down_view.axis("off")
        ax_pred_update_bel.set_title("Predictive belief")
        ax_pred_update_bel.axis("off")
        ax_obs_update_bel.set_title("Obs belief")
        ax_obs_update_bel.axis("off")
        ax_belief.set_title("Belief")
        ax_belief.axis("off")

        # Add data to all plots.
        if self.pano_rgb is not None:
            ax_pano_rgb.imshow(self.pano_rgb)

        if self.current_ground_truth_local_map is not None:
            ax_local_occ_gt.imshow(self.current_ground_truth_local_map, cmap="gray", vmin=0, vmax=1)
            
        if self.current_predicted_local_map is not None:
            ax_local_occ_pred.imshow(self.current_predicted_local_map, cmap="gray", vmin=0, vmax=1)

        if self.predictive_belief_map is not None:
            predictive_belief = self.normalize_belief_for_visualization(self.predictive_belief_map)
            ax_pred_update_bel.imshow(predictive_belief, cmap="gray", vmin=0, vmax=1)

        if self.observation_prob_map is not None:
            observation_belief = self.normalize_belief_for_visualization(self.observation_prob_map)
            ax_obs_update_bel.imshow(observation_belief, cmap="gray", vmin=0, vmax=1)

        if self.agent_belief_map is not None:
            belief = self.normalize_belief_for_visualization(self.agent_belief_map)
            ax_belief.imshow(belief, cmap="gray", vmin=0, vmax=1)

        # Retrieve a view on the renderer buffer
        canvas.draw()
        buf = canvas.buffer_rgba()
        # convert to a NumPy array
        result_img = np.asarray(buf)
        # Convert to the correct color scheme.
        return cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
    