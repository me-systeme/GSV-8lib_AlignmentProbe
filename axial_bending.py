# axial_bending.py
# Helper function to compute axial and bending strain from four gauges

import numpy as np


def axial_bending(e0, e90, e180, e270, eps_min=1e-6):
    """
    Compute axial strain, bending components and bending percentage.

    Parameters
    ----------
    e0, e90, e180, e270 : float
        Strains at 0°, 90°, 180° and 270°.
    eps_min : float, optional
        Minimum absolute axial strain for normalization to avoid division by zero.

    Returns
    -------
    dict
        Keys:
        - eps_ax: axial strain (average of two opposite gauge pairs)
        - eps_bx: bending component in x direction
        - eps_by: bending component in y direction
        - eps_b_mag: magnitude of bending strain
        - phi: angle of bending vector in radians
        - percent_bending: bending ratio in percent (|eps_b| / |eps_ax| * 100)
    """
    eps_ax_1 = (e0 + e180) / 2.0
    eps_ax_2 = (e90 + e270) / 2.0
    eps_ax = 0.5 * (eps_ax_1 + eps_ax_2)  # robust average

    eps_bx = (e0 - e180) / 2.0
    eps_by = (e90 - e270) / 2.0

    eps_b_mag = np.hypot(eps_bx, eps_by)
    phi = np.arctan2(eps_by, eps_bx)  # rad

    denom = max(abs(eps_ax), eps_min)
    percent_bending = 100.0 * eps_b_mag / denom

    return dict(
        eps_ax=eps_ax,
        eps_bx=eps_bx,
        eps_by=eps_by,
        eps_b_mag=eps_b_mag,
        phi=phi,
        percent_bending=percent_bending,
    )