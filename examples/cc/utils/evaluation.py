import ast
import dataclasses
import json
import os
import platform
import traceback
import urllib.request
from argparse import ArgumentParser
from pathlib import Path, PurePosixPath
from typing import Optional

import docker

if platform.system() == "Linux":
    import resource

from swebench.harness.constants import (
    APPLY_PATCH_FAIL,
    APPLY_PATCH_PASS,
    DOCKER_PATCH,
    DOCKER_USER,
    DOCKER_WORKDIR,
    INSTANCE_IMAGE_BUILD_DIR,
    KEY_INSTANCE_ID,
    KEY_MODEL,
    KEY_PREDICTION,
    LOG_INSTANCE,
    LOG_REPORT,
    LOG_TEST_OUTPUT,
    RUN_EVALUATION_LOG_DIR,
    UTF8,
    SWEbenchInstance,
)
from swebench.harness.docker_build import (
    BuildImageError,
    build_container,
    close_logger,
    setup_logger,
)
from swebench.harness.docker_utils import (
    cleanup_container,
    copy_to_container,
    exec_run_with_timeout,
    remove_image,
    should_remove,
)
from swebench.harness.grading import get_eval_report
from swebench.harness.test_spec.test_spec import TestSpec, make_test_spec
from swebench.harness.utils import (
    EvaluationError,
    get_predictions_from_file,
    load_swebench_dataset,
    str2bool,
)

GIT_APPLY_CMDS = [
    "git apply --verbose",
    "git apply --verbose --reject",
    "patch --batch --fuzz=5 -p1 -i",
]

PRO_REPO_RAW_BASE = "https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/main"
PRO_CACHE_DIR = Path(os.environ.get("CC_PRO_HARNESS_DIR", "examples/cc/pro_harness"))
PRO_SCRIPTS_DIR = PRO_CACHE_DIR / "run_scripts"
PRO_DOCKERFILES_DIR = PRO_CACHE_DIR / "dockerfiles"
PRO_BASE_DOCKER_DIR = PRO_DOCKERFILES_DIR / "base_dockerfile"
PRO_INSTANCE_DOCKER_DIR = PRO_DOCKERFILES_DIR / "instance_dockerfile"


def _download_text(url: str, dest: Path) -> None:
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp:
        dest.write_text(resp.read().decode("utf-8"))


def _ensure_pro_artifacts(instance_id: str) -> dict[str, Path]:
    run_script = PRO_SCRIPTS_DIR / instance_id / "run_script.sh"
    parser_script = PRO_SCRIPTS_DIR / instance_id / "parser.py"
    base_dockerfile = PRO_BASE_DOCKER_DIR / instance_id / "Dockerfile"
    instance_dockerfile = PRO_INSTANCE_DOCKER_DIR / instance_id / "Dockerfile"

    _download_text(
        f"{PRO_REPO_RAW_BASE}/run_scripts/{instance_id}/run_script.sh",
        run_script,
    )
    _download_text(
        f"{PRO_REPO_RAW_BASE}/run_scripts/{instance_id}/parser.py",
        parser_script,
    )
    _download_text(
        f"{PRO_REPO_RAW_BASE}/dockerfiles/base_dockerfile/{instance_id}/Dockerfile",
        base_dockerfile,
    )
    _download_text(
        f"{PRO_REPO_RAW_BASE}/dockerfiles/instance_dockerfile/{instance_id}/Dockerfile",
        instance_dockerfile,
    )

    return {
        "run_script": run_script,
        "parser_script": parser_script,
        "base_dockerfile": base_dockerfile,
        "instance_dockerfile": instance_dockerfile,
    }


def _extract_env_exports(dockerfile_text: str) -> list[str]:
    exports = []
    for line in dockerfile_text.splitlines():
        line = line.strip()
        if line.startswith("ENV "):
            exports.append(line.replace("ENV", "export", 1))
    return exports


def _create_pro_entryscript(instance: SWEbenchInstance, base_dockerfile: str, instance_dockerfile: str) -> str:
    if "before_repo_set_cmd" not in instance:
        raise KeyError("before_repo_set_cmd missing from pro instance")
    if "selected_test_files_to_run" not in instance:
        raise KeyError("selected_test_files_to_run missing from pro instance")

    before_repo_set_cmd = instance["before_repo_set_cmd"].strip().split("\n")[-1]
    try:
        selected_tests = ast.literal_eval(instance["selected_test_files_to_run"])
    except Exception as exc:
        raise ValueError("selected_test_files_to_run is not a valid list") from exc
    selected_tests_arg = ",".join(selected_tests)
    base_commit = instance["base_commit"]

    env_cmds = []
    env_cmds.extend(_extract_env_exports(base_dockerfile))
    env_cmds.extend(_extract_env_exports(instance_dockerfile))

    env_block = "\n".join(env_cmds)
    return f"""{env_block}
# apply patch
cd /app
git reset --hard {base_commit}
git checkout {base_commit}
git apply -v /workspace/patch.diff
{before_repo_set_cmd}
# run tests
bash /workspace/run_script.sh {selected_tests_arg} > /workspace/stdout.log 2> /workspace/stderr.log
# parse results
python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json
"""


def _get_dockerhub_image_uri(uid: str, dockerhub_username: str, repo_name: str = "") -> str:
    repo_base, repo_name_only = repo_name.lower().split("/")
    hsh = uid.replace("instance_", "")

    if uid == "instance_element-hq__element-web-ec0f940ef0e8e3b61078f145f34dc40d1938e6c5-vnan":
        repo_name_only = "element-web"
    elif "element-hq" in repo_name.lower() and "element-web" in repo_name.lower():
        repo_name_only = "element"
        if hsh.endswith("-vnan"):
            hsh = hsh[:-5]
    elif hsh.endswith("-vnan"):
        hsh = hsh[:-5]

    tag = f"{repo_base}.{repo_name_only}-{hsh}"
    if len(tag) > 128:
        tag = tag[:128]

    return f"{dockerhub_username}/sweap-images:{tag}"


def _pro_resolved(output: Optional[dict]) -> bool:
    if not output:
        return False
    tests = output.get("tests")
    if not tests:
        return False
    for test in tests:
        status = str(test.get("status", "")).upper()
        if status in {"FAILED", "ERROR"}:
            return False
    return True


def run_instance_pro(
    pred: dict,
    instance: SWEbenchInstance,
    run_id: str,
    timeout: int | None,
    dockerhub_username: str,
) -> tuple[str, dict]:
    instance_id = instance["instance_id"]
    model_name_or_path = pred.get(KEY_MODEL, "None").replace("/", "__")
    eval_logs_root = os.environ.get("CC_EVAL_LOG_DIR")
    if eval_logs_root:
        eval_logs_root = Path(eval_logs_root)
    else:
        eval_logs_root = RUN_EVALUATION_LOG_DIR
    log_dir = eval_logs_root / run_id / model_name_or_path / instance_id
    report_path = log_dir / LOG_REPORT

    if report_path.exists():
        return instance_id, json.loads(report_path.read_text())

    log_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = log_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    artifacts = _ensure_pro_artifacts(instance_id)
    base_dockerfile = artifacts["base_dockerfile"].read_text()
    instance_dockerfile = artifacts["instance_dockerfile"].read_text()
    entryscript = _create_pro_entryscript(instance, base_dockerfile, instance_dockerfile)

    (workspace_dir / "patch.diff").write_text(pred.get(KEY_PREDICTION) or "")
    (workspace_dir / "run_script.sh").write_text(artifacts["run_script"].read_text())
    (workspace_dir / "parser.py").write_text(artifacts["parser_script"].read_text())
    (workspace_dir / "entryscript.sh").write_text(entryscript)

    image = _get_dockerhub_image_uri(instance_id, dockerhub_username, instance.get("repo", ""))
    client = docker.from_env()
    try:
        client.images.pull(image)
    except Exception:
        try:
            client.images.get(image)
        except Exception as exc:
            raise RuntimeError(f"Failed to pull or find image: {image}") from exc

    container = None
    try:
        container = client.containers.run(
            image,
            volumes={str(workspace_dir): {"bind": "/workspace", "mode": "rw"}},
            detach=True,
            entrypoint="/bin/bash",
            command=["-c", "bash /workspace/entryscript.sh"],
        )
        container.wait(timeout=timeout)
    except Exception:
        if container is not None:
            container.kill()
        raise
    finally:
        if container is not None:
            container.remove(force=True)

    stdout_path = workspace_dir / "stdout.log"
    stderr_path = workspace_dir / "stderr.log"
    stdout = stdout_path.read_text() if stdout_path.exists() else ""
    stderr = stderr_path.read_text() if stderr_path.exists() else ""
    (log_dir / "stdout.log").write_text(stdout)
    (log_dir / "stderr.log").write_text(stderr)
    (log_dir / LOG_TEST_OUTPUT).write_text(f"{stdout}\n{stderr}")

    output_path = workspace_dir / "output.json"
    output = json.loads(output_path.read_text()) if output_path.exists() else None
    report = {
        instance_id: {
            "resolved": _pro_resolved(output),
            "output": output,
        }
    }
    report_path.write_text(json.dumps(report, indent=4))
    return instance_id, report


def _override_test_spec_image(test_spec: TestSpec, instance_image_key_override: str) -> TestSpec:
    if not instance_image_key_override:
        return test_spec
    try:
        if dataclasses.is_dataclass(test_spec):
            return dataclasses.replace(
                test_spec,
                instance_image_key=instance_image_key_override,
                is_remote_image=True,
            )
    except Exception:
        pass
    try:
        test_spec.instance_image_key = instance_image_key_override
        test_spec.is_remote_image = True
        return test_spec
    except Exception as exc:  # pragma: no cover - defensive for unknown TestSpec types
        raise RuntimeError("Failed to override instance_image_key on TestSpec") from exc


def run_instance(
    test_spec: TestSpec,
    pred: dict,
    rm_image: bool,
    force_rebuild: bool,
    client: docker.DockerClient,
    run_id: str,
    timeout: int | None = None,
    rewrite_reports: bool = False,
):
    """
    Run a single instance with the given prediction.

    Args:
        test_spec (TestSpec): TestSpec instance
        pred (dict): Prediction w/ model_name_or_path, model_patch, instance_id
        rm_image (bool): Whether to remove the image after running
        force_rebuild (bool): Whether to force rebuild the image
        client (docker.DockerClient): Docker client
        run_id (str): Run ID
        timeout (int): Timeout for running tests
        rewrite_reports (bool): True if eval run is just to reformat existing report
    """
    # Set up logging directory
    instance_id = test_spec.instance_id
    model_name_or_path = pred.get(KEY_MODEL, "None").replace("/", "__")
    eval_logs_root = os.environ.get("CC_EVAL_LOG_DIR")
    if eval_logs_root:
        eval_logs_root = Path(eval_logs_root)
    else:
        eval_logs_root = RUN_EVALUATION_LOG_DIR
    log_dir = eval_logs_root / run_id / model_name_or_path / instance_id

    # Set up report file
    report_path = log_dir / LOG_REPORT
    if rewrite_reports:
        test_output_path = log_dir / LOG_TEST_OUTPUT
        if not test_output_path.exists():
            raise ValueError(f"Test output file {test_output_path} does not exist")
        report = get_eval_report(
            test_spec=test_spec,
            prediction=pred,
            test_log_path=test_output_path,
            include_tests_status=True,
        )
        # Write report to report.json
        with open(report_path, "w") as f:
            f.write(json.dumps(report, indent=4))
        return instance_id, report
    if report_path.exists():
        return instance_id, json.loads(report_path.read_text())

    if not test_spec.is_remote_image:
        # Link the image build dir in the log dir
        build_dir = INSTANCE_IMAGE_BUILD_DIR / test_spec.instance_image_key.replace(":", "__")
        image_build_link = log_dir / "image_build_dir"
        if not image_build_link.exists():
            try:
                # link the image build dir in the log dir
                image_build_link.symlink_to(build_dir.absolute(), target_is_directory=True)
            except:
                # some error, idk why
                pass

    # Set up logger
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_INSTANCE
    logger = setup_logger(instance_id, log_file)

    # Run the instance
    container = None
    try:
        # Build + start instance container (instance image should already be built)
        container = build_container(test_spec, client, run_id, logger, rm_image, force_rebuild)
        container.start()
        logger.info(f"Container for {instance_id} started: {container.id}")

        # Copy model prediction as patch file to container
        patch_file = Path(log_dir / "patch.diff")
        patch_file.write_text(pred[KEY_PREDICTION] or "")
        logger.info(f"Intermediate patch for {instance_id} written to {patch_file}, now applying to container...")
        copy_to_container(container, patch_file, PurePosixPath(DOCKER_PATCH))

        # Attempt to apply patch to container (TODO: FIX THIS)
        applied_patch = False
        for git_apply_cmd in GIT_APPLY_CMDS:
            val = container.exec_run(
                f"{git_apply_cmd} {DOCKER_PATCH}",
                workdir=DOCKER_WORKDIR,
                user=DOCKER_USER,
            )
            if val.exit_code == 0:
                logger.info(f"{APPLY_PATCH_PASS}:\n{val.output.decode(UTF8)}")
                applied_patch = True
                break
            else:
                logger.info(f"Failed to apply patch to container: {git_apply_cmd}")
        if not applied_patch:
            logger.info(f"{APPLY_PATCH_FAIL}:\n{val.output.decode(UTF8)}")
            raise EvaluationError(
                instance_id,
                f"{APPLY_PATCH_FAIL}:\n{val.output.decode(UTF8)}",
                logger,
            )

        # Get git diff before running eval script
        git_diff_output_before = (
            container.exec_run("git -c core.fileMode=false diff", workdir=DOCKER_WORKDIR).output.decode(UTF8).strip()
        )
        logger.info(f"Git diff before:\n{git_diff_output_before}")

        eval_file = Path(log_dir / "eval.sh")
        eval_file.write_text(test_spec.eval_script)
        logger.info(f"Eval script for {instance_id} written to {eval_file}; copying to container...")
        copy_to_container(container, eval_file, PurePosixPath("/eval.sh"))

        # Run eval script, write output to logs
        test_output, timed_out, total_runtime = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout)
        test_output_path = log_dir / LOG_TEST_OUTPUT
        logger.info(f"Test runtime: {total_runtime:_.2f} seconds")
        with open(test_output_path, "w") as f:
            f.write(test_output)
            logger.info(f"Test output for {instance_id} written to {test_output_path}")
            if timed_out:
                f.write(f"\n\nTimeout error: {timeout} seconds exceeded.")
                raise EvaluationError(
                    instance_id,
                    f"Test timed out after {timeout} seconds.",
                    logger,
                )

        # Get git diff after running eval script (ignore permission changes)
        git_diff_output_after = (
            container.exec_run("git -c core.fileMode=false diff", workdir=DOCKER_WORKDIR).output.decode(UTF8).strip()
        )

        # Check if git diff changed after running eval script
        logger.info(f"Git diff after:\n{git_diff_output_after}")
        if git_diff_output_after != git_diff_output_before:
            logger.info("Git diff changed after running eval script")

        # Get report from test output
        logger.info(f"Grading answer for {instance_id}...")
        report = get_eval_report(
            test_spec=test_spec,
            prediction=pred,
            test_log_path=test_output_path,
            include_tests_status=True,
        )
        logger.info(f"report: {report}\n" f"Result for {instance_id}: resolved: {report[instance_id]['resolved']}")

        # Write report to report.json
        with open(report_path, "w") as f:
            f.write(json.dumps(report, indent=4))
        return instance_id, report
    except EvaluationError as e:
        error_msg = traceback.format_exc()
        logger.info(error_msg)
        print(e)
    except BuildImageError as e:
        error_msg = traceback.format_exc()
        logger.info(error_msg)
        print(e)
    except Exception as e:
        error_msg = (
            f"Error in evaluating model for {instance_id}: {e}\n"
            f"{traceback.format_exc()}\n"
            f"Check ({logger.log_file}) for more information."
        )
        logger.error(error_msg)
    finally:
        # Remove instance container + image, close logger
        cleanup_container(client, container, logger)
        if rm_image:
            remove_image(client, test_spec.instance_image_key, logger)
        close_logger(logger)
    return


def evaluate(
    prediction: dict,
    instance: SWEbenchInstance,
    cache_level,
    clean,
    force_rebuild,
    run_id,
    timeout,
    namespace,
    instance_image_tag,
    rewrite_reports,
    instance_image_key_override: str | None = None,
    use_pro_harness: bool = False,
    dockerhub_username: Optional[str] = None,
):
    if use_pro_harness:
        if not dockerhub_username:
            raise ValueError("dockerhub_username is required for swebench_pro evaluation")
        return run_instance_pro(
            prediction,
            instance,
            run_id,
            timeout,
            dockerhub_username,
        )

    client = docker.from_env()
    test_spec = make_test_spec(instance, namespace=namespace, instance_image_tag=instance_image_tag)
    if instance_image_key_override:
        test_spec = _override_test_spec_image(test_spec, instance_image_key_override)

    instance_image_ids = {
        test_spec.instance_image_key,
    }
    existing_images = {tag for i in client.images.list(all=True) for tag in i.tags if tag in instance_image_ids}

    return run_instance(
        test_spec,
        prediction,
        should_remove(test_spec.instance_image_key, cache_level, clean, existing_images),
        force_rebuild,
        client,
        run_id,
        timeout,
        rewrite_reports,
    )


def filter_dataset_by_predictions(predictions, dataset_name, split):
    full_dataset = load_swebench_dataset(dataset_name, split)
    full_dataset_ids = {i[KEY_INSTANCE_ID] for i in full_dataset}

    prediction_ids = set(predictions.keys())
    if prediction_ids - full_dataset_ids:
        raise ValueError(
            (
                "Some prediction IDs not found in dataset!"
                f"\nMissing IDs:\n{' '.join(prediction_ids - full_dataset_ids)}"
            )
        )

    empty_patch_ids = {k for k, v in predictions.items() if v[KEY_PREDICTION] == "" or v[KEY_PREDICTION] is None}
    # filter dataset to only instances with predictions
    dataset = {
        i[KEY_INSTANCE_ID]: i
        for i in full_dataset
        if i[KEY_INSTANCE_ID] in prediction_ids and i[KEY_INSTANCE_ID] not in empty_patch_ids
    }
    return dataset, full_dataset


def main(
    dataset_name: str,
    split: str,
    predictions_path: str,
    force_rebuild: bool,
    cache_level: str,
    clean: bool,
    open_file_limit: int,
    run_id: str,
    timeout: int,
    namespace: str | None,
    rewrite_reports: bool,
    instance_image_tag: str = "latest",
):
    # load predictions as map of instance_id to prediction
    predictions = get_predictions_from_file(predictions_path, dataset_name, split)
    predictions = {pred[KEY_INSTANCE_ID]: pred for pred in predictions}

    dataset, _ = filter_dataset_by_predictions(predictions, dataset_name, split)

    # run instances locally
    if platform.system() == "Linux":
        resource.setrlimit(resource.RLIMIT_NOFILE, (open_file_limit, open_file_limit))

    results = {}
    print(f"Running evaluation for {len(predictions)} predictions...")
    for instance_id in predictions:
        if instance_id not in dataset:
            results[instance_id] = None
            print(f"Skipping instance {instance_id} as it has empty patch...")
            continue

        print(f"Evaluating instance {instance_id}...")
        result = evaluate(
            predictions[instance_id],
            dataset[instance_id],
            cache_level,
            clean,
            force_rebuild,
            run_id,
            timeout,
            namespace=namespace,
            instance_image_tag=instance_image_tag,
            rewrite_reports=rewrite_reports,
        )
        results[instance_id] = result

    with open(f"debug.json", "w") as f:
        json.dump(results, f, indent=4)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument(
        "--dataset_name",
        default="SWE-bench/SWE-bench_Lite",
        type=str,
        help="Name of dataset or path to JSON file.",
    )
    parser.add_argument("--split", type=str, default="test", help="Split of the dataset")
    parser.add_argument(
        "--predictions_path",
        type=str,
        help="Path to predictions file - if 'gold', uses gold predictions",
        required=True,
    )

    # Local execution args
    parser.add_argument("--open_file_limit", type=int, default=4096, help="Open file limit")
    parser.add_argument(
        "--timeout",
        type=int,
        default=1_800,
        help="Timeout (in seconds) for running tests for each instance",
    )
    parser.add_argument(
        "--force_rebuild",
        type=str2bool,
        default=False,
        help="Force rebuild of all images",
    )
    parser.add_argument(
        "--cache_level",
        type=str,
        choices=["none", "base", "env", "instance"],
        help="Cache level - remove images above this level",
        default="env",
    )
    # if clean is true then we remove all images that are above the cache level
    # if clean is false, we only remove images above the cache level if they don't already exist
    parser.add_argument("--clean", type=str2bool, default=False, help="Clean images above cache level")
    parser.add_argument("--run_id", type=str, required=True, help="Run ID - identifies the run")
    parser.add_argument("--namespace", type=str, default="swebench", help="Namespace for images")
    parser.add_argument("--instance_image_tag", type=str, default="latest", help="Instance image tag")
    parser.add_argument(
        "--rewrite_reports",
        type=str2bool,
        default=False,
        help="Doesn't run new instances, only writes reports for instances with existing test outputs",
    )

    # Add arguments to the parser
    args = parser.parse_args()
    main(**vars(args))
