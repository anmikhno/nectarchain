import argparse
import os
import subprocess
import sys

import astropy.units as u
import numpy as np
import numpy.ma as ma
import tabulate as tab
from astropy.io import ascii
from astropy.table import Table
from ctapipe.coordinates import EngineeringCameraFrame
from ctapipe.image.toymodel import Gaussian
from ctapipe.instrument import CameraGeometry
from ctapipe.io import EventSource
from ctapipe.io.hdf5tableio import HDF5TableReader

# ctapipe modules
from ctapipe.visualization import CameraDisplay
from iminuit import Minuit
from jacobi import propagate
from matplotlib import pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

from nectarchain.data.container import GainContainer

parser = argparse.ArgumentParser(description="Run NectarCAM photostatistics analysis")

parser.add_argument("-r", "--run-number", required=True, help="Run number")
parser.add_argument("-s", "--spe-run-number", required=True, help="SPE run number")
parser.add_argument(
    "-p",
    "--run-path",
    default=f'{os.environ.get("NECTARCAMDATA", "").strip()}',
    help="Path to run file",
)
parser.add_argument(
    "-a",
    "--analysis-file",
    default=f'{os.environ.get("NECTARCAMDATA", "").strip()}',
    help="Analysis file name",
)

# Accept True/False as string
parser.add_argument(
    "-v",
    "--add-variance",
    type=str,
    default="False",
    help="Enable or disable variance (True/False)",
)

args = parser.parse_args()

# --- Convert to boolean ---
if args.add_variance.lower() in ("true", "1", "t", "yes"):
    add_variance = True
elif args.add_variance.lower() in ("false", "0", "f", "no"):
    add_variance = False
else:
    raise ValueError(
        f"Invalid value for -v/--add-variance: {args.add_variance}. Use True or False."
    )

# --- Assign other variables ---
run_number = args.run_number
run_spe_number = args.spe_run_number
run_path = args.run_path + f"/runs/NectarCAM.Run{run_number}.0000.fits.fz"
filename_ps = (
    args.analysis_file + f"/PhotoStat/PhotoStatisticNectarCAM_FFrun{run_number}"
    f"_GlobalPeakWindowSum_window_width_8_Pedrun{run_number}_FullWaveformSum.h5"
)

# --- Example usage ---
if not os.path.exists(filename_ps):
    print(f"[INFO] {filename_ps} not found, running gain_PhotoStat_computation.py...")

    gain_script = os.path.expanduser(
        "~/local/src/nectarchain/src/nectarchain/"
        "user_scripts/ggrolleron/gain_PhotoStat_computation.py"
    )

    cmd = [
        sys.executable,
        gain_script,
        "--FF_run_number",
        run_number,
        "--Ped_run_number",
        run_number,
        "--SPE_run_number",
        run_spe_number,
        "--method",
        "GlobalPeakWindowSum",
        "--extractor_kwargs",
        '{"window_width":8}',
        "--overwrite",
        "-v",
        "INFO",
        "--reload_events",
    ]

    print("[DEBUG] Running command:", " ".join(cmd))
    subprocess.run(cmd, check=True)
else:
    print(f"[INFO] File {run_path} already exists, skipping computation.")

print(f"[INFO] Starting analysis on {filename_ps}")
print(f"[DEBUG] ADD_VARIANCE = {add_variance}")

if add_variance:
    print("[INFO] Running analysis with  variance ...")

else:
    print("[INFO] Running without variance...")


def pre_process_fits(filename):
    with HDF5TableReader(filename) as h5_table:
        assert h5_table._h5file.isopen == True
        for container in h5_table.read("/data/GainContainer_0", GainContainer):
            print(container.as_dict())
            break
    h5_table.close()

    total_pixels = 1855

    # Generate the full expected pixel ID list
    expected_pixels = np.arange(total_pixels)
    container_dict = container.as_dict()
    print(f"number of valid pixels : {len(container_dict['is_valid'])}")

    # Find missing pixel IDs
    existing_pixels = container_dict["pixels_id"]
    missing_pixels = np.setdiff1d(expected_pixels, existing_pixels)

    # Determine the shape of the 'high_gain' values
    hg_shape = (
        container_dict["high_gain"].shape[1]
        if len(container_dict["high_gain"].shape) > 1
        else 1
    )
    lg_shape = (
        container_dict["low_gain"].shape[1]
        if len(container_dict["low_gain"].shape) > 1
        else 1
    )
    charge_std_shape = (
        container_dict["charge_std"].shape[1]
        if len(container_dict["charge_std"].shape) > 1
        else 1
    )

    # Create missing entries with zeros matching the correct shape
    missing_entries = {
        "pixels_id": missing_pixels,
        "high_gain": np.zeros((len(missing_pixels), hg_shape)),
        "low_gain": np.zeros((len(missing_pixels), lg_shape)),
        "charge": np.zeros(len(missing_pixels)),  # Ensures same shape as 'high_gain'
        "charge_std": np.zeros((len(missing_pixels), charge_std_shape)),
    }

    # Merge original and missing data
    merged_pixel_ids = np.concatenate([existing_pixels, missing_entries["pixels_id"]])
    merged_hg = np.concatenate(
        [container_dict["high_gain"], missing_entries["high_gain"]], axis=0
    )
    merged_lg = np.concatenate(
        [container_dict["low_gain"], missing_entries["low_gain"]], axis=0
    )
    merged_charge = np.concatenate(
        [container_dict["charge"], missing_entries["charge"]]
    )
    merged_charge_std = np.concatenate(
        [container_dict["charge"], missing_entries["charge"]]
    )

    # Sort by pixel_id to maintain order
    sorted_indices = np.argsort(merged_pixel_ids)
    container_dict["pixels_id"] = merged_pixel_ids[sorted_indices]
    container_dict["high_gain"] = merged_hg[sorted_indices]
    container_dict["low_gain"] = merged_lg[sorted_indices]
    container_dict["charge"] = merged_charge[sorted_indices]
    container_dict["charge_std"] = merged_charge_std[sorted_indices]
    mask_check_hg = [a <= 0 for a in container_dict["high_gain"][:, 0]]
    masked_hg = ma.masked_array(container_dict["high_gain"][:, 0], mask=mask_check_hg)

    high_gain = container_dict["high_gain"][:, 0]
    n_pe = np.divide(
        container_dict["charge"],
        high_gain,
        out=np.zeros_like(high_gain, dtype=float),
        where=high_gain > 0,
    )
    std_n_pe = np.sqrt(
        np.divide(
            container_dict["charge_std"] * n_pe,
            container_dict["charge"],
            out=np.zeros_like(high_gain, dtype=float),
            where=high_gain != 0,
        )
    )

    mask = [a == 0 for a in std_n_pe]

    sigma_masked = ma.masked_array(std_n_pe, mask=mask)
    n_pe = ma.masked_array(n_pe, mask=mask)

    # Perform some plots
    fig0 = plt.figure(figsize=(6, 5))
    ax = plt.subplot()
    disp = CameraDisplay(geometry=camera, show_frame=False)

    disp.image = n_pe
    disp.add_colorbar()
    # disp.set_limits_minmax(140, 165)

    cbar1 = fig0.axes[-1]
    cbar1.set_ylabel(
        r"Illumination, $n_{\rm PE}$", rotation=90, labelpad=15, fontsize=16
    )
    cbar1.tick_params(labelsize=16)  # Increase tick label size on colorbar
    # Axis labels
    ax.set_xlabel("x (m)", fontsize=16)
    ax.set_ylabel("y (m)", fontsize=16)
    # Tick label size
    ax.tick_params(axis="both", which="major", labelsize=16)
    # Title
    plt.title("Data", fontsize=18)
    pdf.savefig(fig0)

    dict_missing_pix = {
        "Missing pixels": len(missing_pixels),
        "high_gain = 0": ma.count_masked(masked_hg) - len(missing_pixels),
    }

    labels = [
        f'Missing pixels, number = {dict_missing_pix["Missing pixels"]}',
        f'high gain = 0, number = {dict_missing_pix["high_gain = 0"]}',
    ]
    print(labels)

    return (
        n_pe,
        std_n_pe,
        sigma_masked,
        dict_missing_pix,
        container_dict["high_gain"],
        container_dict["low_gain"],
        container_dict["charge"],
    )


# Fit using ctapipe
# First step of the fitting process
def Gaussian_model(array=[1000.0, 0.0, 0.0, 1.5, 1.5]):
    A, x, y, std_x, std_y = array
    model = A * (
        Gaussian(x * u.m, y * u.m, std_x * u.m, std_y * u.m, psi="0d").pdf(
            camera.pix_x, camera.pix_y
        )
    )
    return model


# least-squares score function = sum of data residuals squared
def LSQ(a0, a1, a2, a3):
    a4 = a3  # changed
    return np.sum(
        (n_pe - Gaussian_model([a0, a1, a2, a3, a4])) ** 2 / (sigma_masked**2)
    )


def optimize_with_outlier_rejection(sigma, data):
    def define_delete_out(sigma, data):
        mean = np.mean(data)
        std = np.std(data)
        outliers = [np.abs(data - mean) > 3 * std]
        # print(f'Number of outliers to be masked: {outliers.sum()}')

        sigma = ma.masked_array(sigma, mask=outliers)
        data = ma.masked_array(data, mask=outliers)
        return data, sigma, outliers

    # Apply outlier mask based on data
    n_pe, sigma_masked, mask_upd = define_delete_out(sigma, data)

    # Define the least-squares function
    def LSQ_wrap(a0, a1, a2, a3):
        a4 = a3  # changed
        return np.sum(
            (n_pe - Gaussian_model([a0, a1, a2, a3, a4])) ** 2 / (sigma_masked**2)
        )

    # Fit with Minuit using previous best parameters
    minuit = Minuit(LSQ_wrap, a0=1000.0, a1=0.0, a2=0.0, a3=1.5)
    minuit.migrad()

    if not minuit.fmin.is_valid:
        print("Warning: Fit did not converge! Stopping iteration.")
    print(f"covariance table: {tab.tabulate(*minuit.covariance.to_table())}")
    print(
        f"Fit new parameters: amplitude = {minuit.values['a0']}, "
        f"x = {minuit.values['a1']}, y = {minuit.values['a2']}, "
        f"length = {minuit.values['a3']}"
    )

    model = Gaussian_model(
        [
            minuit.values["a0"],
            minuit.values["a1"],
            minuit.values["a2"],
            minuit.values["a3"],
            minuit.values["a3"],
        ]
    )
    residuals = (n_pe - model) / model
    max_residual = np.max(np.abs(residuals))
    print(f"Max residual: {max_residual*100:.2f}%")

    # Visualization
    print(f" number of masked elements for outliers{sigma_masked.count()}")
    fig2 = plt.figure(figsize=(12, 9))

    # --- Subplot 1 ---
    ax1 = plt.subplot(2, 2, 1)
    disp1 = CameraDisplay(geometry=camera, show_frame=False, ax=ax1)
    disp1.image = model
    disp1.add_colorbar()
    # disp1.set_limits_minmax(140, 165)

    # Set colorbar label for subplot 1
    cbar1 = fig2.axes[-1]
    cbar1.set_ylabel(r"$n_{\rm PE}$", rotation=90, labelpad=15, fontsize=14)

    ax1.set_xlabel("x (m)", fontsize=14)
    ax1.set_ylabel("y (m)", fontsize=14)

    ax1.tick_params(axis="both", which="major", labelsize=11)
    # Title
    ax1.set_title("Model", fontsize=16)

    # --- Subplot 2 ---
    ax2 = plt.subplot(2, 2, 2)
    disp2 = CameraDisplay(camera, show_frame=False, ax=ax2)
    disp2.image = residuals * 100
    disp2.cmap = plt.cm.coolwarm
    disp2.add_colorbar()

    # Set colorbar label for subplot 2
    cbar2 = fig2.axes[-1]  # Again, the last axis should be the second colorbar
    cbar2.set_ylabel(r"%", rotation=90, labelpad=15, fontsize=14)

    ax2.set_xlabel("x (m)", fontsize=14)
    ax2.set_ylabel("y (m)", fontsize=14)
    ax2.tick_params(axis="both", which="major", labelsize=11)
    # Title
    ax2.set_title("Residuals", fontsize=16)

    # Save and close
    pdf.savefig(fig2)
    plt.close(fig2)
    return n_pe, model, minuit, residuals


def model(params):
    # params = [A, mu_x, mu_y, sigma]
    sigma_y = params[3]  # enforce sigma_x = sigma_y
    return params[0] * (
        Gaussian(
            params[1] * u.m, params[2] * u.m, params[3] * u.m, sigma_y * u.m, psi="0d"
        ).pdf(camera.pix_x, camera.pix_y)
    )


def error_propagation_compute(data, minuit_resulting, plot=True, rebin=True):
    """Compute both parameter uncertainties and per-pixel uncertainties of the model."""

    # --- Parameters and covariance from Minuit
    values = [minuit_resulting.values[i] for i in range(4)]
    errors = [minuit_resulting.errors[i] for i in range(4)]

    print("Fit results:")
    print(f"A = {values[0]:.2f} ± {errors[0]:.2f}")
    print(f"μ_x = {values[1]:.2f} ± {errors[1]:.2f}")
    print(f"μ_y = {values[2]:.2f} ± {errors[2]:.2f}")
    print(f"σ_x = {values[3]:.2f} ± {errors[3]:.2f}")
    print(f"σ_y = {values[3]:.2f} ± {errors[3]:.2f}")

    if len(minuit_resulting.values) == 6:
        print(
            f"V_int = {minuit_resulting.values[5]:.2f} "
            f"± {minuit_resulting.errors[5]:.2f}"
        )

    # --- Propagate errors through the model
    y, ycov = propagate(
        lambda p: model(p), minuit_resulting.values, minuit_resulting.covariance
    )  # changed
    yerr_prop = np.sqrt(np.diag(ycov))  # per-pixel uncertainty

    # --- Optionally rebin by θ
    if rebin:
        theta = np.rad2deg(
            np.sqrt(
                (camera.pix_x.value - values[1]) ** 2
                + (camera.pix_y.value - values[2]) ** 2
            )
            / 12
        )
        bins = np.arange(np.min(theta), np.max(theta) + 0.1, 0.1)

        sum_y, _ = np.histogram(theta, bins=bins, weights=y)
        sum_y_err, _ = np.histogram(theta, bins=bins, weights=yerr_prop)
        count, _ = np.histogram(theta, bins=bins)

        rebinned_y = np.where(count > 0, sum_y / count, np.nan)
        rebinned_y_err = np.where(count > 0, sum_y_err / count, np.nan)
        bin_centers = 0.5 * (bins[:-1] + bins[1:])
    else:
        rebinned_y, rebinned_y_err, bin_centers = None, None, None

    # --- Optional plotting
    if plot and rebin:
        fig_model = plt.figure(figsize=(6, 5))
        ax = plt.subplot()
        ax.scatter(theta, data, label="data", zorder=0, alpha=0.3)
        ax.fill_between(
            bin_centers,
            rebinned_y - rebinned_y_err,
            rebinned_y + rebinned_y_err,
            facecolor="C1",
            alpha=0.5,
        )
        ax.plot(bin_centers, rebinned_y, color="r", label="model")
        plt.ylabel("Number of photoelectrons")
        plt.xlabel("θ [deg]")
        plt.legend()
        pdf.savefig(fig_model)

    return {
        "params": values,
        "param_errors": errors,
        "model_values": y,
        "model_errors": yerr_prop,
        "rebinned": (bin_centers, rebinned_y, rebinned_y_err),
    }


def characterize_peak(minuit, fit):
    """Compute coordinated of the peak of
    the fitted Gaussian and corresponding pixel."""
    dist_squared = (camera.pix_x.value - minuit[1]) ** 2 + (
        camera.pix_y.value - minuit[2]
    ) ** 2

    # Find index of minimum distance
    closest_pixel_id = np.argmin(dist_squared)

    # Optional: get the actual closest coordinates
    closest_x = camera.pix_x.value[closest_pixel_id]
    closest_y = camera.pix_y.value[closest_pixel_id]
    distance = np.sqrt(closest_x**2 + closest_y**2)

    print(f"Closest pixel ID: {closest_pixel_id}")
    print(f"Coordinates: ({closest_x}, {closest_y})")
    print(
        f"The distance between the centre of the camera "
        f"and the peak of the fitted 2D gaussian: {distance:.3f} meters"
    )

    # disp = CameraDisplay(geometry=camera)
    # disp.image = fit
    # disp.add_colorbar()
    # disp.highlight_pixels(closest_pixel_id, color="red")

    return distance


# Same fit procedure but taking into account V_int


# least-squares score function = sum of data residuals squared
def LS_variance(a0, a1, a2, a3, v_int):
    a4 = a3  # changed
    return np.sum(
        (n_pe - Gaussian_model([a0, a1, a2, a3, a4])) ** 2 / (sigma_masked**2 + v_int)
        - np.log(sigma_masked**2 / (sigma_masked**2 + v_int))
    )


# values for minuit are taken from the firs fit without any
def optimize_with_outlier_rejection_variance(sigma, data, minuit):
    def define_delete_out(sigma, data):
        mean = np.mean(data)
        std = np.std(data)
        outliers = [np.abs(data - mean) > 3 * std]
        # print(f'Number of outliers to be masked: {outliers.sum()}')

        sigma = ma.masked_array(sigma, mask=outliers)
        data = ma.masked_array(data, mask=outliers)
        return data, sigma, outliers

    # Update data, sigma, and mask
    n_pe, sigma_masked, mask_upd = define_delete_out(sigma, data)

    # Define the least-squares function
    def LSQ_wrap_var(array_parameters):
        A, x, y, std_x, v_int = array_parameters  # changed
        std_y = std_x  # changed
        return np.sum(
            (n_pe - Gaussian_model([A, x, y, std_x, std_y])) ** 2
            / (sigma_masked**2 + v_int)
            - np.log(sigma_masked**2 / (sigma_masked**2 + v_int))
        )

    # Initialize Minuit with updated function and parameters
    minuit_new = Minuit(LSQ_wrap_var, minuit)
    minuit_new.limits["x0"] = (0, None)  # A>0
    minuit_new.limits["x3"] = (0, None)  # std_x > 0
    minuit_new.limits["x4"] = (0, None)  # V_int > 0
    minuit_new.migrad()

    print(f"covariance table: {tab.tabulate(*minuit_new.covariance.to_table())}")
    print(
        f"Fit new parameters: amplitude = {minuit_new.values['x0']},"
        f" x = {minuit_new.values['x1']}, y = {minuit_new.values['x2']},"
        f" length = {minuit_new.values['x3']}, "
        f" intrinsic variance = {minuit_new.values['x4']}"
    )  # changed

    model = Gaussian_model(
        [
            minuit_new.values["x0"],
            minuit_new.values["x1"],
            minuit_new.values["x2"],
            minuit_new.values["x3"],
            minuit_new.values["x3"],
        ]
    )
    residuals = (n_pe - model) / model
    max_residual = np.max(np.abs(residuals))
    print(f"Max residual: {max_residual*100:.2f}%")

    dict_missing_pix["rejected_outliers"] = (
        1855
        - sigma_masked.count()
        - dict_missing_pix["Missing pixels"]
        - dict_missing_pix["high_gain = 0"]
    )

    labels = [
        f'Missing pixels, #{dict_missing_pix["Missing pixels"]}',
        f'high gain = 0, # {dict_missing_pix["high_gain = 0"]}',
        f'rejected outliers, # {dict_missing_pix["rejected_outliers"]}',
    ]

    fig_pie_3, ax = plt.subplots()
    ax.pie(dict_missing_pix.values(), labels=labels, autopct="%.0f%%")
    fig_pie_3.suptitle("Piechart of masked pixels")
    pdf.savefig(fig_pie_3)

    # Visualization

    fig4 = plt.figure(figsize=(16, 8))
    # --- Subplot 1 ---
    ax1 = plt.subplot(2, 2, 1)
    disp1 = CameraDisplay(camera, show_frame=False, ax=ax1)
    disp1.image = model
    disp1.add_colorbar()

    # Set colorbar label for subplot 1
    cbar1 = fig4.axes[-1]
    cbar1.set_ylabel(r"$n_{\rm PE}$", rotation=90, labelpad=15, fontsize=14)
    ax1.set_xlabel("x (m)", fontsize=14)
    ax1.set_ylabel("y (m)", fontsize=14)
    ax1.tick_params(axis="both", which="major", labelsize=11)
    ax1.set_title("Model", fontsize=16)

    # --- Subplot 2 ---
    ax2 = plt.subplot(2, 2, 2)
    disp2 = CameraDisplay(camera, show_frame=False, ax=ax2)
    disp2.image = residuals * 100
    disp2.cmap = plt.cm.coolwarm
    disp2.add_colorbar()
    cbar2 = fig4.axes[-1]
    cbar2.set_ylabel(r"%", rotation=90, labelpad=15, fontsize=14)
    ax2.set_xlabel("x (m)", fontsize=14)
    ax2.set_ylabel("y (m)", fontsize=14)
    ax2.tick_params(axis="both", which="major", labelsize=11)
    ax2.set_title("Residuals", fontsize=16)
    pdf.savefig(fig4)
    plt.close(fig4)

    return n_pe, model, minuit_new, residuals


def compute_ff_coefs(charges, gains):
    print(f"SHAPEE {gains.shape}")
    masked_charges = np.ma.masked_where(np.ma.getmask(charges), charges)
    masked_gains = np.ma.masked_where(np.ma.getmask(charges), gains)

    relative_signal = np.divide(
        masked_charges, masked_gains[:, 0], where=gains[:, 0] != 0
    )
    eff = relative_signal / np.mean(relative_signal)
    mean = np.mean(eff)
    std = np.std(eff)

    fig_ff_1, ax = plt.subplots(figsize=(8, 5))

    # Plot histogram
    n, bins, patches = ax.hist(
        eff,
        bins=50,
        edgecolor="black",
        alpha=0.7,
    )

    # Add vertical lines for mean and ±σ
    ax.axvline(
        mean, color="red", linestyle="solid", linewidth=2, label=f"Mean = {mean:.2f}"
    )
    ax.axvline(
        mean - std,
        color="red",
        linestyle="dashed",
        linewidth=1.5,
        label=f"±1σ = {std:.2f}",
    )
    ax.axvline(mean + std, color="red", linestyle="dashed", linewidth=1.5)
    ax.set_title(
        f"Distribution of FF coefficient, run {run_number}, model independent",
        fontsize=16,
    )
    ax.set_xlabel("FF coefficient", fontsize=16)
    ax.set_ylabel("Count", fontsize=16)
    ax.tick_params(axis="both", labelsize=16)
    ax.legend(fontsize=16)
    ax.grid(True, linestyle="--", alpha=0.5)
    fig_ff_1.tight_layout()
    pdf.savefig(fig_ff_1)

    return eff


def compute_ff_coefs_model(data, data_std, model, model_std):
    FF_coefs = np.divide(data, model, where=model != 0)
    mean = np.mean(FF_coefs)
    std = np.std(FF_coefs)
    std_FF = np.sqrt((data_std / model) ** 2 + (data * model_std / model**2) ** 2)
    fig_ff, ax = plt.subplots(figsize=(8, 5))

    # Plot histogram
    n, bins, patches = ax.hist(
        FF_coefs,
        bins=50,
        edgecolor="black",
        alpha=0.7,
    )
    # Add vertical lines for mean and ±σ
    ax.axvline(
        mean, color="red", linestyle="solid", linewidth=2, label=f"Mean = {mean:.2f}"
    )
    ax.axvline(
        mean - std,
        color="red",
        linestyle="dashed",
        linewidth=1.5,
        label=f"±1σ = {std:.2f}",
    )
    ax.axvline(mean + std, color="red", linestyle="dashed", linewidth=1.5)
    ax.set_title(
        f"Distribution of FF coefficient, run {run_number}, model-based", fontsize=16
    )
    ax.set_xlabel("FF coefficient", fontsize=16)
    ax.set_ylabel("Count", fontsize=16)
    ax.tick_params(axis="both", labelsize=16)
    ax.legend(fontsize=16)
    ax.grid(True, linestyle="--", alpha=0.5)
    fig_ff.tight_layout()
    pdf.savefig(fig_ff)
    return FF_coefs, std_FF


# main
if __name__ == "__main__":
    camera = CameraGeometry.from_name("NectarCam-003").transform_to(
        EngineeringCameraFrame()
    )

    # Create PdfPages object
    pdf = PdfPages(f"Plots_analysis_run{run_number}.pdf")

    source = EventSource.from_url(input_url=run_path, max_events=100)
    len(source)
    for event in source:
        print(event.index.event_id, event.trigger.event_type, event.trigger.time)

    # Looking for broken pixels
    fig00 = plt.figure(13, figsize=(5, 5))
    disp = CameraDisplay(geometry=camera, show_frame=False)
    chan = 0
    disp.image = event.mon.tel[0].pixel_status.hardware_failing_pixels[chan]
    disp.set_limits_minmax(0, 1)
    disp.cmap = plt.cm.coolwarm
    disp.add_colorbar()
    fig00.suptitle("Broken/missing pixels")
    pdf.savefig(fig00)

    (
        n_pe,
        std_n_pe,
        sigma_masked,
        dict_missing_pix,
        high_gains,
        low_gains,
        charges,
    ) = pre_process_fits(filename_ps)

    # First fit no variance
    data_1, fit_1, minuit_1, residuals_1 = optimize_with_outlier_rejection(
        sigma_masked, n_pe
    )
    dict_errors = error_propagation_compute(data_1, minuit_1)
    y_1, yerr_prop_1, minuit_vals_1, minuit_vals_errors_1 = (
        dict_errors["model_values"],
        dict_errors["model_errors"],
        dict_errors["params"],
        dict_errors["param_errors"],
    )
    print(f"Resulting error for the model is {np.mean(yerr_prop_1/y_1)*100:.2f}%")
    characterize_peak(minuit_vals_1, fit_1)
    print(minuit_1.values)

    # Visualize how many pixels were masked
    dict_missing_pix["rejected_outliers"] = (
        1855
        - sigma_masked.count()
        - dict_missing_pix["Missing pixels"]
        - dict_missing_pix["high_gain = 0"]
    )

    labels = [
        f'Missing pixels, #{dict_missing_pix["Missing pixels"]}',
        f'high gain = 0, # {dict_missing_pix["high_gain = 0"]}',
        f'rejected outliers, # {dict_missing_pix["rejected_outliers"]}',
    ]
    fig_pie, ax = plt.subplots()
    ax.pie(dict_missing_pix.values(), labels=labels, autopct="%.0f%%")
    fig_pie.suptitle("Piechart of masked pixels")
    pdf.savefig(fig_pie)

    # Second fit with variance
    if add_variance == True:
        (
            data_varinace,
            fit_variance,
            minuit_variance_result,
            residuals_variance_result,
        ) = optimize_with_outlier_rejection_variance(
            sigma_masked,
            n_pe,
            [
                minuit_1.values["a0"],
                minuit_1.values["a1"],
                minuit_1.values["a2"],
                minuit_1.values["a3"],
                0.0,
            ],
        )

        plt.figure()
        plt.hist(residuals_variance_result, bins=50)
        plt.title("Residuals binned", fontsize=16)
        plt.close()

        dict_error_var = error_propagation_compute(
            data_varinace, minuit_variance_result, plot=True, rebin=True
        )

        (
            y_variance,
            yerr_prop_variance,
            minuit_values_variance,
            minuit_values_error_variance,
        ) = (
            dict_error_var["model_values"],
            dict_error_var["model_errors"],
            dict_error_var["params"],
            dict_error_var["param_errors"],
        )

        print(
            f"Resulting error for the model is "
            f"{np.mean(yerr_prop_variance / y_variance) * 100:.2f}%"
        )
        print(np.min(camera.pix_x.value))
        print(np.min(camera.pix_y.value))
        print(np.mean(camera.pix_x.value))
        print(minuit_values_variance[1])
        print(minuit_values_variance[2])

        characterize_peak(minuit_values_variance, fit_variance)

    # compute flat field coef
    simple_ff_coefs = compute_ff_coefs(charges, high_gains)
    ff_coefs_model, ff_coefs_model_err = compute_ff_coefs_model(
        n_pe, std_n_pe, y_1, yerr_prop_1
    )

    with open(f"Log_info_run_{run_number}_fixed.txt", "a") as f:
        # Write a header if file is empty (i.e. first time writing)
        if f.tell() == 0:
            f.write(
                "Run,Model,A,x0(rad),y0(rad),width(rad),"
                "v_int,A_err,x0_err(rad),y0_err(rad),width_err(rad),"
                "v_int_err, model_error\n"
            )

        # Convert angles
        x0_1 = np.arctan(minuit_vals_1[1] / 12)
        y0_1 = np.arctan(minuit_vals_1[2] / 12)
        width_1 = np.arctan(minuit_vals_1[3] / 12)

        x0_1_err = np.arctan(minuit_vals_errors_1[1] / 12)
        y0_1_err = np.arctan(minuit_vals_errors_1[2] / 12)
        width_1_err = np.arctan(minuit_vals_errors_1[3] / 12)

        if add_variance == True:
            x0_v = np.arctan(minuit_values_variance[1] / 12)
            y0_v = np.arctan(minuit_values_variance[2] / 12)
            width_v = np.arctan(minuit_values_variance[3] / 12)

            x0_v_err = np.arctan(minuit_values_error_variance[1] / 12)
            y0_v_err = np.arctan(minuit_values_error_variance[2] / 12)
            width_v_err = np.arctan(minuit_values_error_variance[3] / 12)

        # First model (without v_int)
        f.write(
            f"{run_number},Initial,{minuit_vals_1[0]},{x0_1},{y0_1},{width_1},,"
            f"{minuit_vals_errors_1[0]},{x0_1_err},"
            f"{y0_1_err},{width_1_err},{np.mean(yerr_prop_1/y_1)*100}\n"
        )
        if add_variance == True:
            f.write(
                f"{run_number},With_v_int,{minuit_values_variance[0]},"
                f"{x0_v},{y0_v},{width_v},"
                f"{minuit_values_error_variance[0]},{x0_v_err},"
                f"{y0_v_err},{width_v_err}, "
                f"{np.mean(yerr_prop_variance / y_variance) * 100}\n"
            )

    data = Table()
    data["pixel_id"] = camera.pix_id
    data["x"] = camera.pix_x
    data["y"] = camera.pix_y
    data["N_photoelectrons_fited"] = y_1
    data["N_photoelectrons_std_fited"] = yerr_prop_1
    data["FF_coef_independent_way"] = simple_ff_coefs
    data["FF_coef_model_way"] = ff_coefs_model
    data["FF_coef_model_way_err"] = ff_coefs_model_err
    data["high_gain_init"] = high_gains[:, 0]
    data["low_gain_init"] = low_gains[:, 0]
    data["Charge_init"] = charges
    data["N_photoelectrons_init"] = n_pe
    data["N_photoelectrons_std_init"] = std_n_pe
    ascii.write(data, f"FF_calibration_run{run_number}.dat", overwrite=True)

    pdf.close()
