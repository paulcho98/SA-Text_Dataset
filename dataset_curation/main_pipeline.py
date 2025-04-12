import argparse
import yaml
import os
import logging
import time
import shutil
import sys
import re

# Import pipeline steps from src
from src import utils
from src import bridge_runner
from src import cropping
from src import vlm_processing
from src import filtering
from src import formatting


# Helper function for timing steps
def time_step(step_name, func, *args, **kwargs):
    """Times a pipeline step and logs the duration using the ROOT logger (for file)."""
    logging.info(f"--- Starting: {step_name} ---")  # Goes to root logger (file)
    step_start_time = time.time()
    result = func(*args, **kwargs)
    step_end_time = time.time()
    duration = step_end_time - step_start_time
    logging.info(f"--- Finished: {step_name} (Duration: {duration:.2f} seconds) ---")  # Goes to root logger (file)
    if result is None:
        logging.error(f"--- Step Failed: {step_name} ---")  # Goes to root logger (file)
        raise RuntimeError(f"Step '{step_name}' failed.")
    return result


def main():
    parser = argparse.ArgumentParser(description="SA-1B Text Restoration Dataset Curation Pipeline")
    parser.add_argument("--config", required=True, help="Path to the base configuration YAML file")
    parser.add_argument("--sa1b_subfolder", type=str, default=None, help="Override the 'sa1b_subfolder' from the config file (e.g., 'sa_000001')")
    parser.add_argument("--output_suffix", type=str, default=None, help="Suffix to append to output directory names (e.g., '_000001')")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility") # Add seed argument
    args = parser.parse_args()

    # --- Load Configuration FIRST ---
    try:
        with open(args.config, 'r') as f:
            config = yaml.safe_load(f)
        print(f"Loaded base configuration from: {args.config}")
    except Exception as e:
        print(f"FATAL: Error loading config file {args.config}: {e}")
        sys.exit(1)

    # --- Apply Overrides & Determine Suffix ---
    overridden_keys = []
    if args.sa1b_subfolder:
        config['sa1b_subfolder'] = args.sa1b_subfolder
        overridden_keys.append('sa1b_subfolder')

    if args.output_suffix:
        output_suffix_raw = args.output_suffix
        overridden_keys.append('output_suffix')
    else:
        output_suffix_raw = f"_{config.get('sa1b_subfolder', 'default')}"

    # Sanitize the suffix
    output_suffix = output_suffix_raw.replace('/', '_').replace('\\', '_')
    # output_suffix = re.sub(r'[\\/*?:"<>| ]', '_', output_suffix_raw) # Optional regex

    # --- Define Paths (incorporating suffix) ---
    sa1b_input_dir = os.path.join(config['sa1b_base_dir'], config['sa1b_subfolder'])
    intermediate_base = config['intermediate_output_base_dir'] + output_suffix
    final_dataset_output_dir = config['final_dataset_dir'] + output_suffix
    intermediate_dir = os.path.join(intermediate_base, "intermediate")
    crop_image_dir = os.path.join(intermediate_base, "cropped_images")

    # --- Setup Logging AFTER determining output paths ---
    utils.setup_logging(config, intermediate_base)  # Log file saved in intermediate base

    # --- Set Random Seed ---
    utils.seed_everything(args.seed) # Call seed_everything here

    logging.info("Pipeline Started.")  # Goes to root logger (file)
    pipeline_start_time = time.time()
    logging.info(f"Using configuration: {config}")
    if overridden_keys:
        logging.info(f"Applied overrides for: {', '.join(overridden_keys)}")
    logging.info(f"Using output suffix: '{output_suffix}'")
    logging.info(f"Intermediate base directory: {intermediate_base}")
    logging.info(f"Final dataset directory: {final_dataset_output_dir}")

    # --- Ensure Output Dirs Exist ---
    utils.ensure_dir(intermediate_base)
    utils.ensure_dir(intermediate_dir)
    utils.ensure_dir(crop_image_dir)
    utils.ensure_dir(final_dataset_output_dir)

    # Define intermediate/final file paths
    stage1_json = os.path.join(intermediate_dir, f"bridge_stage1_results{output_suffix}.json")
    stage2_json = os.path.join(intermediate_dir, f"bridge_stage2_results{output_suffix}.json")
    vlm1_raw_json = os.path.join(intermediate_dir, f"{config['vlm1_name']}_raw{output_suffix}.json")
    vlm2_raw_json = os.path.join(intermediate_dir, f"{config['vlm2_name']}_raw{output_suffix}.json")
    vlm1_filtered_json = os.path.join(intermediate_dir, f"{config['vlm1_name']}_filtered{output_suffix}.json")
    vlm2_filtered_json = os.path.join(intermediate_dir, f"{config['vlm2_name']}_filtered{output_suffix}.json")
    combined_json = os.path.join(intermediate_dir, f"vlm_combined{output_suffix}.json")
    agreed_list_txt = os.path.join(intermediate_dir, f"agreed_image_list{output_suffix}.txt")
    agreed_json = os.path.join(intermediate_dir, f"agreed_annotations{output_suffix}.json")
    blur_csv = os.path.join(intermediate_dir, f"blur_assessment{output_suffix}.csv")
    tagged_json_intermediate = os.path.join(intermediate_dir, f"tagged_annotations_intermediate{output_suffix}.json")
    restoration_json_intermediate = os.path.join(intermediate_dir, f"restoration_annotations_intermediate{output_suffix}.json")
    full_dataset_final_json = os.path.join(final_dataset_output_dir, f"full_dataset{output_suffix}.json")
    restoration_dataset_final_json = os.path.join(final_dataset_output_dir, f"restoration_dataset{output_suffix}.json")

    pipeline_steps = {}

    try:
        # --- Steps 3-14 (Pipeline Logic) ---
        stage1_output_dir = os.path.join(intermediate_dir, "bridge_stage1")
        pipeline_steps['stage1_json_temp'] = time_step(
            f"Step 1: Bridge Detection (Stage 1) [{config['sa1b_subfolder']}]",
            bridge_runner.run_bridge, config, sa1b_input_dir, stage1_output_dir, stage1=True
        )
        expected_stage1_output = os.path.join(stage1_output_dir, "text_detection_results.json")
        if os.path.exists(expected_stage1_output):
            shutil.move(expected_stage1_output, stage1_json)
            pipeline_steps['stage1_json'] = stage1_json
            logging.info(f"Stage 1 results moved to: {stage1_json}")
            if not config.get('keep_intermediate_files', True):
                shutil.rmtree(stage1_output_dir, ignore_errors=True)
        elif os.path.exists(stage1_json):
            logging.warning(f"Target Stage 1 JSON {stage1_json} already exists. Using existing file.")
            pipeline_steps['stage1_json'] = stage1_json
        else:
            raise RuntimeError(f"Bridge Stage 1 output JSON not found at expected location: {expected_stage1_output}")

        crop_definitions = time_step(
            f"Step 2: Define Crop Regions [{config['sa1b_subfolder']}]",
            cropping.define_crop_regions, pipeline_steps['stage1_json'], config
        )
        time_step(
            f"Step 3: Create Crop Images [{config['sa1b_subfolder']}]",
            cropping.create_crop_images, crop_definitions, sa1b_input_dir, crop_image_dir, config
        )
        pipeline_steps['crop_image_dir'] = crop_image_dir

        stage2_output_dir = os.path.join(intermediate_dir, "bridge_stage2")
        pipeline_steps['stage2_json_temp'] = time_step(
            f"Step 4: Bridge Detection (Stage 2 on Crops) [{config['sa1b_subfolder']}]",
            bridge_runner.run_bridge, config, pipeline_steps['crop_image_dir'], stage2_output_dir, stage1=False
        )
        expected_stage2_output = os.path.join(stage2_output_dir, "text_detection_results.json")
        if os.path.exists(expected_stage2_output):
            shutil.move(expected_stage2_output, stage2_json)
            pipeline_steps['stage2_json'] = stage2_json
            logging.info(f"Stage 2 results moved to: {stage2_json}")
            if not config.get('keep_intermediate_files', True):
                shutil.rmtree(stage2_output_dir, ignore_errors=True)
        elif os.path.exists(stage2_json):
            logging.warning(f"Target Stage 2 JSON {stage2_json} already exists. Using existing file.")
            pipeline_steps['stage2_json'] = stage2_json
        else:
            raise RuntimeError(f"Bridge Stage 2 output JSON not found at expected location: {expected_stage2_output}")

        pipeline_steps['vlm1_raw_json'] = time_step(
            f"Step 5: VLM Recognition ({config['vlm1_name']}) [{config['sa1b_subfolder']}]",
            vlm_processing.run_vlm_recognition, config['vlm1_name'], pipeline_steps['stage2_json'], pipeline_steps['crop_image_dir'], vlm1_raw_json, config
        )
        pipeline_steps['vlm2_raw_json'] = time_step(
            f"Step 6: VLM Recognition ({config['vlm2_name']}) [{config['sa1b_subfolder']}]",
            vlm_processing.run_vlm_recognition, config['vlm2_name'], pipeline_steps['stage2_json'], pipeline_steps['crop_image_dir'], vlm2_raw_json, config
        )
        pipeline_steps['vlm1_filtered_json'] = time_step(
            f"Step 7: Filter Empty VLM Results ({config['vlm1_name']}) [{config['sa1b_subfolder']}]",
            filtering.filter_empty_vlm, pipeline_steps['vlm1_raw_json'], vlm1_filtered_json, config
        )
        pipeline_steps['vlm2_filtered_json'] = time_step(
            f"Step 8: Filter Empty VLM Results ({config['vlm2_name']}) [{config['sa1b_subfolder']}]",
            filtering.filter_empty_vlm, pipeline_steps['vlm2_raw_json'], vlm2_filtered_json, config
        )
        pipeline_steps['combined_json'] = time_step(
            f"Step 9: Compare & Merge VLMs [{config['sa1b_subfolder']}]",
            filtering.compare_and_merge_vlms, pipeline_steps['vlm1_filtered_json'], pipeline_steps['vlm2_filtered_json'], combined_json, config
        )
        pipeline_steps['agreed_list_txt'] = time_step(
            f"Step 10: Identify Fully Agreed Images [{config['sa1b_subfolder']}]",
            filtering.identify_agreed_images, pipeline_steps['combined_json'], agreed_list_txt, config
        )
        pipeline_steps['agreed_json'] = time_step(
            f"Step 11: Extract Agreed Annotations [{config['sa1b_subfolder']}]",
            filtering.extract_agreed_annotations, pipeline_steps['combined_json'], pipeline_steps['agreed_list_txt'], agreed_json, config
        )

        agreed_filenames_list = utils.read_text_list(pipeline_steps['agreed_list_txt'])
        if agreed_filenames_list is None:
            raise RuntimeError("Failed to read agreed image list for blur assessment.")
        pipeline_steps['blur_csv'] = time_step(
            f"Step 12: Assess Crop Blurriness (Agreed Crops) [{config['sa1b_subfolder']}]",
            vlm_processing.run_blur_assessment, agreed_filenames_list, pipeline_steps['crop_image_dir'], blur_csv, config
        )

        pipeline_steps['tagged_json_intermediate'] = time_step(
            f"Step 13a: Tag Images with Blur Category [{config['sa1b_subfolder']}]",
            filtering.tag_with_blur, pipeline_steps['agreed_json'], pipeline_steps['blur_csv'], tagged_json_intermediate, config
        )
        pipeline_steps['restoration_json_intermediate'] = time_step(
            f"Step 13b: Filter Tagged Data by Blur [{config['sa1b_subfolder']}]",
            filtering.filter_tagged_by_blur, pipeline_steps['tagged_json_intermediate'], restoration_json_intermediate, config
        )

        pipeline_steps['full_dataset_final_json'] = time_step(
            f"Step 14a: Final Formatting (Full Dataset) [{config['sa1b_subfolder']}]",
            formatting.format_final_dataset, pipeline_steps['tagged_json_intermediate'], full_dataset_final_json, config
        )
        pipeline_steps['restoration_dataset_final_json'] = time_step(
            f"Step 14b: Final Formatting (Restoration Dataset) [{config['sa1b_subfolder']}]",
            formatting.format_final_dataset, pipeline_steps['restoration_json_intermediate'], restoration_dataset_final_json, config
        )

        logging.info(f"--- Pipeline Completed Successfully for {config['sa1b_subfolder']} ---")
        logging.info(f"Full dataset (tagged) saved to: {pipeline_steps['full_dataset_final_json']}")
        logging.info(f"Restoration dataset (filtered) saved to: {pipeline_steps['restoration_dataset_final_json']}")

    except Exception as e:
        logging.error(f"Pipeline failed for {config.get('sa1b_subfolder', 'N/A')}: {e}", exc_info=True)

    finally:
        # --- Cleanup ---
        if not config.get('keep_intermediate_files', True):
            logging.info(f"Cleaning up intermediate files for {config.get('sa1b_subfolder', 'N/A')}...")
            files_to_remove = [
                stage1_json, stage2_json, vlm1_raw_json, vlm2_raw_json,
                vlm1_filtered_json, vlm2_filtered_json, combined_json,
                agreed_list_txt, agreed_json, blur_csv,
                tagged_json_intermediate, restoration_json_intermediate
            ]
            dirs_to_remove = [
                os.path.join(intermediate_dir, "bridge_stage1"),
                os.path.join(intermediate_dir, "bridge_stage2")
            ]
            for f_path in files_to_remove:
                if f_path and os.path.exists(f_path):
                    try:
                        os.remove(f_path)
                        logging.debug(f"Removed intermediate file: {f_path}")
                    except OSError as clean_e:
                        logging.warning(f"Could not remove intermediate file {f_path}: {clean_e}")
            for d_path in dirs_to_remove:
                if d_path and os.path.exists(d_path):
                    try:
                        shutil.rmtree(d_path, ignore_errors=True)
                        logging.debug(f"Removed intermediate directory: {d_path}")
                    except OSError as clean_e:
                        logging.warning(f"Could not remove intermediate dir {d_path}: {clean_e}")

        pipeline_end_time = time.time()
        logging.info(f"Total pipeline execution time for {config.get('sa1b_subfolder', 'N/A')}: {pipeline_end_time - pipeline_start_time:.2f} seconds.")
        logging.shutdown()  # Ensure logs are flushed

if __name__ == "__main__":
    main()