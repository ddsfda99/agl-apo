import asyncio
import json
import os
import platform
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml

if platform.system() == "Linux":
    import resource

from datasets import Dataset
from swebench.harness.utils import load_swebench_dataset
from transformers import AutoTokenizer as AutoProcessor
from utils.claude_code_controller import ClaudeController
from utils.custom_adapter import LlmProxyTraceToAugmentedTriplet
from utils.custom_callbacks import AddLogprobs, AddTemperature
from utils.evaluation import evaluate
from utils.logger import logger
from utils.type import AgentResult

from agentlightning import (
    InMemoryLightningStore,
    LightningStoreServer,
    LitAgentRunner,
    OtelTracer,
    configure_logger,
)
from agentlightning.litagent import LitAgent
from agentlightning.llm_proxy import LLMProxy, ModelConfig
from agentlightning.types import LLM, AttemptedRollout, NamedResources, ProxyLLM, Rollout, RolloutRawResult, PromptTemplate


def load_dataset(path: str = "swe_debug.jsonl", epoch: int = 0, limit: Optional[int] = None) -> Dict[str, Any]:
    instances = []
    with open(path) as f:
        for line in f:
            instance = json.loads(line)
            instance["epoch"] = epoch
            instances.append(instance)

    if limit is not None:
        instances = instances[:limit]
    return instances


def get_dockerhub_image_uri(uid: str, dockerhub_username: str, repo_name: str = "") -> str:
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


class CodingAgent(LitAgent):
    def __init__(
        self,
        namespace: str = "swebench",
        full_set: str = "princeton-nlp/SWE-bench",
        split: str = "test",
        max_step: int = 5,
        run_method: Literal["python", "cli"] = "cli",
        tools: list[str] = ["Glob", "Grep", "Bash", "Read", "Edit", "Write", "TodoWrite", "WebFetch", "ExitPlanMode"],
        user_prompt: str = "{description}",
        open_file_limit: int = 4096,
        cache_level: str = "env",  # ["none", "base", "env", "instance"]
        clean: bool = False,
        force_rebuild: bool = False,
        timeout: int = 1_800,  # in sec
        instance_image_tag: str = "latest",
        rewrite_reports: bool = False,
        image_uri_mode: Literal["swebench", "swebench_pro"] = "swebench",
        dockerhub_username: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.namespace = namespace
        self.full_set = full_set
        self.split = split
        self.max_step = max_step
        self.run_method = run_method

        self.cache_level = cache_level
        self.clean = clean
        self.force_rebuild = force_rebuild
        self.timeout = timeout
        self.instance_image_tag = instance_image_tag
        self.rewrite_reports = rewrite_reports
        self.image_uri_mode = image_uri_mode
        self.dockerhub_username = dockerhub_username

        full_dataset = load_swebench_dataset(full_set, split)
        self.dataset = {each["instance_id"]: each for each in full_dataset}

        self.tools = tools
        self.user_prompt = user_prompt

        # run instances locally
        if platform.system() == "Linux":
            resource.setrlimit(resource.RLIMIT_NOFILE, (open_file_limit, open_file_limit))

    def _get_instance_image(self, task: Dict[str, Any]) -> str:
        if self.image_uri_mode == "swebench_pro":
            if not self.dockerhub_username:
                raise ValueError("dockerhub_username is required when image_uri_mode is swebench_pro")
            repo_name = task.get("repo") or self.dataset.get(task["instance_id"], {}).get("repo")
            if not repo_name:
                raise KeyError("repo")
            return get_dockerhub_image_uri(task["instance_id"], self.dockerhub_username, repo_name)
        if self.image_uri_mode == "swebench":
            return f"{self.namespace}/sweb.eval.x86_64.{task['instance_id'].lower()}".replace("__", "_1776_")
        raise ValueError(f"Unsupported image_uri_mode: {self.image_uri_mode}")

    async def rollout_async(
        self, task: Dict[str, Any], resources: NamedResources, rollout: Rollout
    ) -> RolloutRawResult:
        run_suffix = os.environ.get("CC_RUN_TS")
        if run_suffix:
            run_id = f"epoch_{task.get('epoch', 0)}_{run_suffix}"
        else:
            run_id = f"epoch_{task.get('epoch', 0)}"
        image = self._get_instance_image(task)

        # ===== Debug: Print all received resources =====
        import logging as _logging
        _logging.info(f"📦 Received resources: {list(resources.keys()) if resources else 'None'}")
        _logging.info(f"📦 Resources type: {type(resources)}")
        _logging.info(f"📦 Resources content: {resources}")
        for k, v in (resources.items() if hasattr(resources, 'items') else []):
            _logging.info(f"  - Resource key: {k}, type: {type(v)}, value: {v}")

        llm = resources.get("llm")
        _logging.info(f"📦 LLM resource: {llm}")
        if llm is None:
            _logging.error("❌ LLM resource is missing! All resources: %s", resources)
        assert llm is not None, "LLM resource is required for rollout."

        llm = self._strip_proxy_helper(llm, rollout)
        
        # Get prompt template from resources (this is what APO optimizes)
        try:
            prompt_template_resource = self._get_prompt_template_resource(resources)
            # Extract the template string from PromptTemplate object
            user_prompt_str = prompt_template_resource.template
            _logging.info(f"✅ Using prompt template from resources")
            # ===== DEBUG: 输出完整的prompt_template =====
            print("\n" + "=" * 80)
            print("🔍 DEBUG [cc_agent.py]: prompt_template received from resources:")
            print("=" * 80)
            print(user_prompt_str)
            print("=" * 80 + "\n")
            # ===== END DEBUG =====
        except Exception as e:
            _logging.error(f"❌ Failed to get prompt_template from resources: {e}")
            _logging.warning(f"⚠️  Falling back to default user_prompt: {self.user_prompt}")
            user_prompt_str = self.user_prompt

        base_prompt_dir = Path(__file__).resolve().parent / "full_prompts"
        base_prompt_dir.mkdir(parents=True, exist_ok=True)
        safe_description = task.get("problem_statement", "").replace('"""', "'''")
        try:
            full_prompt = user_prompt_str.format(description=safe_description)
        except Exception:
            full_prompt = user_prompt_str

        instance_id = task.get("instance_id", "unknown")
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(instance_id))
        prefix = f"{safe_id}_v"
        next_version = 0
        try:
            for name in os.listdir(base_prompt_dir):
                if not name.startswith(prefix) or not name.endswith(".txt"):
                    continue
                suffix = name[len(prefix):-4]
                if suffix.isdigit():
                    next_version = max(next_version, int(suffix) + 1)
        except FileNotFoundError:
            pass

        prompt_dump_path = base_prompt_dir / f"{safe_id}_v{next_version}.txt"
        try:
            with open(prompt_dump_path, "w", encoding="utf-8") as f:
                f.write(full_prompt)
            _logging.info(f"✅ Saved full prompt to {prompt_dump_path}")
        except Exception as e:
            _logging.warning(f"⚠️  Failed to save full prompt to {prompt_dump_path}: {e}")

        prediction: Optional[AgentResult] = None
        try:
            # 1. init container
            controller = ClaudeController(
                image,
                task,
                run_id,
                set(self.tools),
                user_prompt_str,  # Use the prompt from resources, not self.user_prompt
                llm.endpoint,
                llm.api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN", "dummy"),
            )
            # 2. execute task
            prediction: AgentResult = controller.run_instance(task, max_step=self.max_step, run_method=self.run_method)
            logger(run_id, task["instance_id"], json.dumps(prediction, indent=4))
            del controller
        except Exception as e:
            logger(run_id, task["instance_id"], f"Exception during rollout: {e}")
            _logging.error(f"❌ Rollout failed with exception: {e}", exc_info=True)
            return 0.0  # Return zero reward on exception

        # 3. obtain rewards (evaluation result)
        reward = 0.0
        # empty patch
        if prediction["model_patch"] in ["", None]:
            return reward

        instance_id = prediction["instance_id"]

        instance = self.dataset.get(instance_id) or task
        instance_image_key_override = image if self.image_uri_mode == "swebench_pro" else None
        result = evaluate(
            prediction,
            instance,
            self.cache_level,
            self.clean,
            self.force_rebuild,
            run_id,
            self.timeout,
            namespace=self.namespace,
            instance_image_tag=self.instance_image_tag,
            rewrite_reports=self.rewrite_reports,
            instance_image_key_override=instance_image_key_override,
            use_pro_harness=self.image_uri_mode == "swebench_pro",
            dockerhub_username=self.dockerhub_username,
        )

        # error patch
        if result is None:
            return reward

        report = result[1]
        # resolved/unresolved patch
        if report[instance_id]["resolved"]:
            reward = 1.0
        return reward

    def _strip_proxy_helper(self, proxy_llm: LLM, rollout: Rollout) -> LLM:
        """Convert [`ProxyLLM`][agentlightning.ProxyLLM] instances into concrete LLMs.

        It resolves ProxyLLM instances to their concrete LLM implementation
        by attaching the attempted rollout context. This is only used when the function
        signature accepts an `llm` parameter and strip_proxy is True.

        Args:
            proxy_llm: Candidate LLM resource.
            rollout: Rollout metadata that provides rollout and attempt identifiers.

        Returns:
            [`LLM`][agentlightning.LLM] with rollout context baked into the endpoint.

        Raises:
            ValueError: If the rollout is not an
                [`AttemptedRollout`][agentlightning.AttemptedRollout].
        """

        if not isinstance(proxy_llm, ProxyLLM):
            # Not a ProxyLLM, nothing to strip here.
            return proxy_llm

        # Rollout is still a Rollout here because API is not stabilized yet.
        # In practice, it must be an AttemptedRollout.
        if not isinstance(rollout, AttemptedRollout):
            raise ValueError("Rollout is not an AttemptedRollout.")

        return proxy_llm.with_attempted_rollout(rollout)

    def _get_prompt_template_resource(
        self, resources: NamedResources
    ) -> PromptTemplate:
        prompt_template = resources.get("prompt_template")
        if prompt_template is None:
            raise ValueError("PromptTemplate resource 'prompt_template' is required for rollout.")
        return prompt_template


def flatten_messages(messages: List[Any]) -> List[Dict[str, str]]:
    flattened: List[Dict[str, str]] = []
    for msg in messages:
        if msg["role"] in ["system", "user"] and isinstance(msg["content"], list):
            msg_content: List[str] = []
            for content in msg["content"]:
                msg_content.append(content["text"])

            msg["content"] = "".join(msg_content)
        elif msg["role"] == "assistant" and "tool_calls" in msg:
            # NOTE:
            # Tool calls are list of dict, though in most case only one tool call is made per call
            # We serialize it as json string here to avoid nested structure
            msg["tool_calls"] = json.dumps(msg["tool_calls"])

        for k in msg:
            assert isinstance(msg[k], str), f"\n>>> {msg}"
        flattened.append(msg)
    return flattened


async def cc_agent_dry_run_sample(
    model_path,
    server_address,
    sonnet_name,
    haiku_name,
    output_dir,
    config,
) -> None:
    """Run a dry run of the cc agent on a single sample.

    This is a simple test function that runs the math agent on the first 4 problems
    using a single worker. Useful for testing the setup and configuration.
    """

    dataset = load_dataset(config["dataset"]["dataset_path"], limit=4)
    tokenizer = AutoProcessor.from_pretrained(model_path)

    logging = configure_logger(name="Claude Code Agent")

    tracer = OtelTracer()
    runner = LitAgentRunner(tracer)
    adapter = LlmProxyTraceToAugmentedTriplet()
    store = LightningStoreServer(InMemoryLightningStore(), host="0.0.0.0", port=7654)
    llm_proxy = LLMProxy(
        port=12358, store=store, callbacks=["return_token_ids", "opentelemetry", AddLogprobs, AddTemperature]
    )

    await store.start()

    llm_proxy.update_model_list(
        [
            ModelConfig(
                model_name=f"{sonnet_name}",
                litellm_params={
                    "model": f"hosted_vllm/{model_path}",
                    "api_base": server_address,
                },
            ),
            ModelConfig(
                model_name=f"{haiku_name}",
                litellm_params={
                    "model": f"hosted_vllm/{model_path}",
                    "api_base": server_address,
                },
            ),
        ]
    )
    await llm_proxy.restart()

    # Put the LLM proxy address into the store as an address
    await store.add_resources(
        {
            "llm": llm_proxy.as_resource(model="local"),
        }
    )

    agent = CodingAgent(
        namespace=config["dataset"]["namespace"],
        full_set=config["dataset"]["full_set"],
        split=config["dataset"]["split"],
        max_step=config["runtime"]["max_step"],
        run_method=config["runtime"]["run_method"],
        tools=config["agent"]["tools"],
        user_prompt=config["agent"]["user_prompt"],
        image_uri_mode=config["dataset"].get("image_uri_mode", "swebench"),
        dockerhub_username=config["dataset"].get("dockerhub_username"),
    )

    with runner.run_context(agent=agent, store=store):
        rollout = await runner.step(
            dataset[0],
        )

        spans = await store.query_spans(rollout.rollout_id)
        triplets = adapter.adapt(spans)
        logging.info(f"dump {len(spans)} spans, extract {len(triplets)} triplets")
        if output_dir is not None:
            instance_output_dir = f"{output_dir}-{dataset[0]['instance_id']}"
            os.makedirs(instance_output_dir, exist_ok=True)
            with open(
                os.path.join(
                    instance_output_dir, f"stream_{dataset[0]['instance_id']}-{rollout.attempt.attempt_id}.json"
                ),
                "w",
            ) as f:
                for span in spans:
                    f.write(json.dumps(span.model_dump()) + "\n")

            all_triplets: List[Dict[str, Any]] = []
            recent_reward: Optional[float] = None
            for triplet in reversed(triplets):
                if triplet.reward is not None:
                    recent_reward = triplet.reward

                prompt = tokenizer.decode(triplet.prompt["token_ids"])  # type: ignore
                all_triplets.append(
                    {
                        "repo": rollout.input["repo"],
                        "instance_id": rollout.input["instance_id"],
                        "turn": triplet.metadata["sequence_id"],
                        "prompt_ids": triplet.prompt["token_ids"],
                        "gold_completion_ids": triplet.response["token_ids"],
                        "logprobs": triplet.response["logprobs"],
                        "reward": recent_reward,
                        "prompt": prompt,
                        "messages": flatten_messages(triplet.metadata["messages"]),
                    }
                )

            ds = Dataset.from_list(all_triplets)
            ds.save_to_disk(os.path.join(instance_output_dir, f"dataset-{dataset[0]['instance_id']}"))
            logging.info(f"Saved dataset with {len(ds)} samples to dataset-{dataset[0]['instance_id']}")

    await llm_proxy.stop()


async def gold_cc_agent_run_dataset(
    sonnet_name,
    haiku_name,
    output_dir,
    config,
):
    """Run a dry run of the cc agent on a single sample.

    This is a simple test function that runs the math agent on the first 4 problems
    using a single worker. Useful for testing the setup and configuration.
    """
    # Load dataset from config to avoid hard-coded path
    dataset = load_dataset(config["dataset"]["dataset_path"])

    logging = configure_logger(name="Claude Code Agent")

    tracer = OtelTracer()
    runner = LitAgentRunner(tracer)
    store = LightningStoreServer(InMemoryLightningStore(), host="0.0.0.0", port=7654)
    llm_proxy = LLMProxy(
        port=12358,
        store=store,
        callbacks=[
            "opentelemetry",
        ],
    )

    await store.start()
    sleep_seconds = config.get("runtime", {}).get("sleep_seconds", 6)

    # Use CloudGPT configuration (same as cc_apo_algo.py)
    from utils.cloudgpt_aoai import get_openai_token_provider
    token_provider = get_openai_token_provider()
    
    llm_proxy.update_model_list(
        [
            ModelConfig(
                model_name=f"{sonnet_name}",
                litellm_params={
                    "model": "azure/gpt-5-20250807",
                    "api_base": "https://cloudgpt-openai.azure-api.net/",
                    "api_version": "2025-04-01-preview",
                    "azure_ad_token": token_provider(),
                },
            ),
            ModelConfig(
                model_name=f"{haiku_name}",
                litellm_params={
                    "model": "azure/gpt-5-nano-20250807",
                    "api_base": "https://cloudgpt-openai.azure-api.net/",
                    "api_version": "2025-04-01-preview",
                    "azure_ad_token": token_provider(),
                },
            ),
        ]
    )
    await llm_proxy.restart()

    # Put the LLM proxy address into the store as an address
    await store.add_resources(
        {
            "llm": llm_proxy.as_resource(model="local"),
        }
    )

    for each in dataset:
        agent = CodingAgent(
            namespace=config["dataset"]["namespace"],
            full_set=config["dataset"]["full_set"],
            split=config["dataset"]["split"],
            max_step=config["runtime"]["max_step"],
            run_method=config["runtime"]["run_method"],
            tools=config["agent"]["tools"],
            user_prompt=config["agent"]["user_prompt"],
            image_uri_mode=config["dataset"].get("image_uri_mode", "swebench"),
            dockerhub_username=config["dataset"].get("dockerhub_username"),
        )
        with runner.run_context(agent=agent, store=store):
            rollout = await runner.step(each)
            spans = await store.query_spans(rollout.rollout_id)

        if output_dir is None:
            logging.info(f"instance {each['instance_id']} generate {len(spans)} spans")
        else:
            instance_output_dir = f"{output_dir}-{each['instance_id']}"
            os.makedirs(instance_output_dir, exist_ok=True)
            logging.info(f"instance {each['instance_id']} dump {len(spans)} spans to {instance_output_dir}")
            with open(os.path.join(instance_output_dir, f"{each['instance_id']}.json"), "w") as f:
                for span in spans:
                    f.write(json.dumps(span.model_dump()) + "\n")

        time.sleep(sleep_seconds)


if __name__ == "__main__":
    from argparse import ArgumentParser

    parser = ArgumentParser()
    # extract spans from official Claude Code
    parser.add_argument("--official", action="store_true", help="Whether to run official claude code.")

    # extract spans from hosted LLM server via litellm proxy
    parser.add_argument(
        "--model_name_or_path", type=str, default="Qwen/Qwen3-Coder-30B-A3B-Instruct", help="Model name or path."
    )
    parser.add_argument("--server_address", type=str, default="http://localhost:8000/v1", help="LLM server address.")

    # common setup
    parser.add_argument(
        "--sonnet_name", type=str, default="claude-sonnet-4-5-20250929", help="Name of the sonnet model."
    )
    parser.add_argument("--haiku_name", type=str, default="claude-haiku-4-5-20251001", help="Name of the haiku model.")
    parser.add_argument("--output_dir", type=str, default="data", help="Directory to save output logs.")
    parser.add_argument("--agent_config", type=str, default="agent_config.yaml", help="Configs to run claude code.")

    args = parser.parse_args()

    run_ts = time.strftime("%Y%m%d_%H%M%S")
    os.environ.setdefault("CC_RUN_TS", run_ts)
    logs_root = f"logs_{run_ts}"
    os.environ.setdefault("CC_LOGS_DIR", logs_root)
    os.environ.setdefault("CC_EVAL_LOG_DIR", os.path.join(logs_root, "run_evaluation"))

    with open(args.agent_config) as f:
        config = yaml.safe_load(f)

    if args.output_dir is not None:
        args.output_dir = f"{args.output_dir}_{run_ts}"
        os.makedirs(args.output_dir, exist_ok=True)

    if not args.official:
        asyncio.run(
            cc_agent_dry_run_sample(
                model_path=args.model_name_or_path,
                server_address=args.server_address,
                sonnet_name=args.sonnet_name,
                haiku_name=args.haiku_name,
                output_dir=args.output_dir,
                config=config,
            )
        )
    else:
        asyncio.run(
            gold_cc_agent_run_dataset(
                sonnet_name=args.sonnet_name,
                haiku_name=args.haiku_name,
                output_dir=args.output_dir,
                config=config,
            )
        )
