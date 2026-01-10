import importlib.util
import json
import logging
import os
import platform
import sys
import tempfile
import traceback
import urllib.request
import zipfile
from argparse import ArgumentParser
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

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

LIVE_TIMEOUT = 40 * 60


def _normalize_cmds(cmds: Any) -> str:
    if cmds is None:
        return ""
    if isinstance(cmds, str):
        return cmds
    if isinstance(cmds, (list, tuple)):
        return " ; ".join([str(cmd) for cmd in cmds if cmd])
    return str(cmds)


def _normalize_test_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return [value]
            if isinstance(parsed, list):
                return parsed
        return [value]
    return [str(value)]


def _cleanup_eval_containers(client: docker.DockerClient, instance_id: str, logger: logging.Logger | None) -> None:
    prefix = f"sweb.eval.{instance_id.lower()}"
    try:
        containers = client.containers.list(all=True, filters={"name": prefix})
    except Exception as exc:
        msg = f"Failed to list eval containers for {instance_id}: {exc}"
        if logger:
            logger.warning(msg)
        else:
            print(msg)
        return

    for container in containers:
        cleanup_container(client, container, logger)


def _load_launch_from_repo(repo_root: Path):
    runtime_path = repo_root / "launch" / "core" / "runtime.py"
    parser_path = repo_root / "launch" / "scripts" / "parser.py"
    if not runtime_path.exists() or not parser_path.exists():
        return None

    runtime_spec = importlib.util.spec_from_file_location("repolaunch_runtime", runtime_path)
    parser_spec = importlib.util.spec_from_file_location("repolaunch_parser", parser_path)
    if runtime_spec is None or parser_spec is None:
        return None
    runtime_module = importlib.util.module_from_spec(runtime_spec)
    parser_module = importlib.util.module_from_spec(parser_spec)
    assert runtime_spec.loader is not None
    assert parser_spec.loader is not None
    sys.modules[runtime_spec.name] = runtime_module
    sys.modules[parser_spec.name] = parser_module
    runtime_spec.loader.exec_module(runtime_module)
    parser_spec.loader.exec_module(parser_module)
    return runtime_module.SetupRuntime, parser_module.run_parser


def _ensure_launch_available():
    try:
        from launch.core.runtime import SetupRuntime
        from launch.scripts.parser import run_parser
        return SetupRuntime, run_parser
    except Exception:
        pass

    repo_path = os.environ.get("REPOLAUNCH_PATH")
    if repo_path:
        loaded = _load_launch_from_repo(Path(repo_path))
        if loaded is not None:
            return loaded

    cache_root = Path(os.environ.get("CC_REPOLAUNCH_CACHE", ".repolaunch")).resolve()
    repo_root = cache_root / "RepoLaunch-main"
    runtime_path = repo_root / "launch" / "core" / "runtime.py"
    parser_path = repo_root / "launch" / "scripts" / "parser.py"
    if not runtime_path.exists() or not parser_path.exists():
        cache_root.mkdir(parents=True, exist_ok=True)
        url = "https://github.com/microsoft/RepoLaunch/archive/refs/heads/main.zip"
        with urllib.request.urlopen(url) as response, tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(response.read())
            tmp_path = tmp.name
        with zipfile.ZipFile(tmp_path) as zf:
            zf.extractall(cache_root)
        os.unlink(tmp_path)

    loaded = _load_launch_from_repo(repo_root)
    if loaded is not None:
        return loaded

    raise RuntimeError(
        "RepoLaunch package 'launch' is required for SWE-bench-Live evaluation. "
        "Install with: pip install git+https://github.com/microsoft/RepoLaunch.git "
        "or set REPOLAUNCH_PATH to a local RepoLaunch checkout."
    )


def _parse_log_pytest(log: str) -> dict[str, str]:
    class TestStatus(Enum):
        FAILED = "FAILED"
        PASSED = "PASSED"
        SKIPPED = "SKIPPED"
        ERROR = "ERROR"
        XFAIL = "XFAIL"

    test_status_map: dict[str, str] = {}
    for line in log.split("\n"):
        if any(line.startswith(status.value) for status in TestStatus):
            if line.startswith(TestStatus.FAILED.value):
                line = line.replace(" - ", " ")
            test_case = line.split()
            if len(test_case) <= 1:
                continue
            test_status_map[test_case[1]] = test_case[0]
    return test_status_map


def _default_pytest_parser(log: str) -> dict[str, str]:
    mapping = _parse_log_pytest(log)
    normalized: dict[str, str] = {}
    for test, status in mapping.items():
        lowered = status.lower()
        if "pass" in lowered:
            normalized[test] = "pass"
        elif "skip" in lowered:
            normalized[test] = "skip"
        else:
            normalized[test] = "fail"
    return normalized


def _get_default_image_name(
    instance_id: str, platform_name: Literal["windows", "linux"], namespace: str | None
) -> str:
    med = "x86_64" if platform_name == "linux" else "win"
    name = instance_id.replace("__", "_1776_").lower()
    prefix = f"{namespace}/" if namespace else ""
    return f"{prefix}sweb.eval.{med}.{name}"


def _apply_solution_patch_best_effort(solution_patch: str, container, platform_name: Literal["windows", "linux"]) -> None:
    if not solution_patch.strip():
        return
    if platform_name == "linux":
        container.send_command("cd /testbed")
        container.send_command(
            """[ -d .git ] || { g=$(find . -maxdepth 2 -mindepth 2 -type d -name .git -print -quit); [ -n "$g" ] && cd "${g%/.git}"; } ;"""
        )
        container.apply_patch(solution_patch, verbose=True)
        container.send_command("cd /testbed")
    else:
        container.send_command(r"cd C:\testbed")
        container.send_command(
            r"""if (-not (Test-Path .git)) { $g = Get-ChildItem -Directory -Recurse -Depth 2 -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq '.git' } | Select-Object -First 1; if ($g) { Set-Location $g.Parent.FullName } };"""
        )
        container.apply_patch(solution_patch, verbose=True)
        container.send_command(r"cd C:\testbed")


def _evaluate_instance_live(
    instance: dict,
    prediction: dict,
    platform_name: Literal["windows", "linux"],
    output_dir: Path,
    namespace: str | None,
    instance_image_tag: str | None,
    overwrite: bool,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"
    if report_path.exists() and not overwrite:
        try:
            report = json.loads(report_path.read_text())
            if report.get("resolved") is not None:
                return report
        except Exception:
            pass

    SetupRuntime, run_parser = _ensure_launch_available()
    instance_id = instance["instance_id"]
    image = instance.get("docker_image") or _get_default_image_name(instance_id, platform_name, namespace)
    if instance_image_tag and ":" not in image:
        image = f"{image}:{instance_image_tag}"

    container = None
    try:
        container = SetupRuntime.from_launch_image(image, instance_id, platform_name)
        test_patch = instance.get("test_patch", "")
        if test_patch.strip():
            container.apply_patch(test_patch)
        _apply_solution_patch_best_effort(prediction.get(KEY_PREDICTION, ""), container, platform_name)

        rebuild_cmd = _normalize_cmds(instance.get("rebuild_cmds", []))
        if rebuild_cmd.strip():
            container.send_command(rebuild_cmd, timeout=LIVE_TIMEOUT)

        test_cmd = _normalize_cmds(instance.get("test_cmds", []))
        print_cmd = _normalize_cmds(instance.get("print_cmds", []))
        if not print_cmd.strip():
            if platform_name == "linux":
                container.send_command(f"cat > run_test.sh <<'CC_PROMPT'\n{test_cmd}\nCC_PROMPT\n")
                test_cmd = "bash run_test.sh > testlog.out 2>&1"
                print_cmd = "cat testlog.out"
            else:
                container.send_command(
                    "@'\n" + test_cmd + "\n'@ | Out-File -FilePath run_test.ps1 -Encoding UTF8"
                )
                test_cmd = "powershell -ExecutionPolicy Bypass -File run_test.ps1 > testlog.out 2>&1"
                print_cmd = "type testlog.out"

        container.send_command(test_cmd, timeout=LIVE_TIMEOUT)
        post_patch_log = container.send_command(print_cmd).output
        (output_dir / "post_patch_log.txt").write_text(post_patch_log)

        parser_name = (instance.get("log_parser") or instance.get("parser") or "").strip().lower()
        if parser_name == "pytest":
            post_patch_status = _default_pytest_parser(post_patch_log)
        else:
            post_patch_status = run_parser(parser_name, post_patch_log)

        (output_dir / "status.json").write_text(json.dumps(post_patch_status, indent=2))

        pass_to_pass = _normalize_test_list(instance.get("PASS_TO_PASS"))
        fail_to_pass = _normalize_test_list(instance.get("FAIL_TO_PASS"))
        passed = {test for test, status in post_patch_status.items() if "pass" in status}
        failed = {test for test, status in post_patch_status.items() if "fail" in status}

        report = {
            "instance_id": instance_id,
            "resolved": False,
            "PASS_TO_PASS": {
                "success": list(passed & set(pass_to_pass)),
                "failure": list(failed & set(pass_to_pass)),
            },
            "FAIL_TO_PASS": {
                "success": list(passed & set(fail_to_pass)),
                "failure": list(failed & set(fail_to_pass)),
            },
        }
        if (
            len(report["PASS_TO_PASS"]["failure"]) == 0
            and len(report["FAIL_TO_PASS"]["failure"]) == 0
            and len(report["FAIL_TO_PASS"]["success"]) > 0
        ):
            report["resolved"] = True

        report_path.write_text(json.dumps(report, indent=2))
        return report
    finally:
        if container is not None:
            try:
                container.cleanup()
            except Exception as exc:
                logging.getLogger(__name__).warning("Failed to cleanup eval container: %s", exc)


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
    _cleanup_eval_containers(client, instance_id, logger)

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


def evaluate_swebench(
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
):
    client = docker.from_env()
    try:
        test_spec = make_test_spec(instance, namespace=namespace, instance_image_tag=instance_image_tag)
    except KeyError as exc:
        logger = logging.getLogger(__name__)
        if isinstance(instance, dict):
            instance_id = instance.get(KEY_INSTANCE_ID)
            repo = instance.get("repo")
            version = instance.get("version")
        else:
            instance_id = None
            repo = None
            version = None
        logger.warning(
            "Skipping evaluation for unsupported repo/version (instance_id=%s repo=%s version=%s): %s",
            instance_id,
            repo,
            version,
            exc,
        )
        return None

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
    backend: str | None = None,
):
    backend_name = (backend or os.environ.get("CC_EVAL_BACKEND", "live")).lower()
    if backend_name in {"live", "repolaunch", "swebench-live"}:
        if not isinstance(instance, dict):
            raise ValueError("SWE-bench-Live evaluation requires instance data as a dict.")
        instance_id = prediction.get(KEY_INSTANCE_ID) or instance.get(KEY_INSTANCE_ID)
        if not instance_id:
            raise ValueError("Missing instance_id for SWE-bench-Live evaluation.")
        if "PASS_TO_PASS" not in instance or "FAIL_TO_PASS" not in instance:
            raise ValueError("SWE-bench-Live evaluation requires PASS_TO_PASS/FAIL_TO_PASS in instance data.")
        if not _normalize_cmds(instance.get("test_cmds", [])).strip():
            raise ValueError("SWE-bench-Live evaluation requires test_cmds in instance data.")

        model_name_or_path = prediction.get(KEY_MODEL, "None").replace("/", "__")
        eval_logs_root = os.environ.get("CC_EVAL_LOG_DIR")
        eval_logs_root = Path(eval_logs_root) if eval_logs_root else RUN_EVALUATION_LOG_DIR
        log_dir = eval_logs_root / run_id / model_name_or_path / instance_id
        platform_name = instance.get("platform") or ("linux" if platform.system() == "Linux" else "windows")

        try:
            report = _evaluate_instance_live(
                instance=instance,
                prediction=prediction,
                platform_name=platform_name,
                output_dir=log_dir,
                namespace=namespace,
                instance_image_tag=instance_image_tag,
                overwrite=bool(rewrite_reports),
            )
        except Exception as exc:
            logging.getLogger(__name__).error(
                "SWE-bench-Live evaluation failed for %s: %s", instance_id, exc, exc_info=True
            )
            return None
        return instance_id, {instance_id: report}

    if backend_name in {"swebench", "legacy"}:
        return evaluate_swebench(
            prediction,
            instance,
            cache_level,
            clean,
            force_rebuild,
            run_id,
            timeout,
            namespace,
            instance_image_tag,
            rewrite_reports,
        )

    raise ValueError(f"Unknown evaluation backend: {backend_name}")


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
    backend: str | None = None,
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
            backend=backend,
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
    parser.add_argument("--namespace", type=str, default="starryzhang", help="Namespace for images")
    parser.add_argument("--instance_image_tag", type=str, default="latest", help="Instance image tag")
    parser.add_argument(
        "--backend",
        type=str,
        default=os.environ.get("CC_EVAL_BACKEND", "live"),
        choices=["live", "repolaunch", "swebench-live", "swebench", "legacy"],
        help="Evaluation backend (default: live via RepoLaunch).",
    )
    parser.add_argument(
        "--rewrite_reports",
        type=str2bool,
        default=False,
        help="Doesn't run new instances, only writes reports for instances with existing test outputs",
    )

    # Add arguments to the parser
    args = parser.parse_args()
    main(**vars(args))
