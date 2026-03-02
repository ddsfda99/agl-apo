"""
APO Algorithm with LLMProxy, responsible for:
1. Connecting to the Store
2. Initializing the APO algorithm
3. Running prompt optimization
4. Saving the best results
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncAzureOpenAI

import agentlightning as agl
from agentlightning import PromptTemplate
from agentlightning.algorithm.apo import APO
from agentlightning.llm_proxy import LLMProxy, ModelConfig
from agentlightning.adapter.messages import TraceToMessages
from utils.cloudgpt_aoai import get_openai_token_provider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_dataset(path: str = "swe_debug.jsonl", limit: Optional[int] = None) -> List[Dict[str, Any]]:
    with open(path) as f:
        instances = [json.loads(line) for line in f]
    return instances[:limit] if limit else instances

def split_dataset(
    dataset: List[Dict[str, Any]],
    train_size: int,
    val_size: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    return dataset[:train_size], dataset[train_size:train_size + val_size]

# seed prompt
SEED_PROMPT = """You are given a code repository in the current directory (/testbed).
    The bug description is:
    {description}
    =================================================
    You task is to fix the bug with the following steps:
    (1) write test cases to reproduce the bug.
    (2) explore the source codes to locate the bug.
    (3) edit the source codes to fix the bug.
    (4) rerun your written test cases to validate that the bug is fixed. If not, go back to explore the source codes and fix the codes again.
    (5) remember to delete the test cases you write at last.
    Please do not commit your edits. We will do it later."""

async def apo_algorithm(*, store: agl.LightningStore):
    # ===== Original setup (using Azure OpenAI) =====
    # start llm proxy
    llm_proxy = LLMProxy(
        port=12358,
        store=store,
        callbacks=[
            "opentelemetry",
        ],
    )
    
    # ===== CloudGPT setup =====
    token_provider = get_openai_token_provider()
    
    llm_proxy.update_model_list(
        [
            ModelConfig(
                model_name="claude-sonnet-4-5-20250929",
                litellm_params={
                    "model": "azure/gpt-5-mini-20250807",
                    "api_base": "https://cloudgpt-openai.azure-api.net/",
                    "api_version": "2025-04-01-preview",
                    "azure_ad_token": token_provider(),
                },
            ),
            ModelConfig(
                model_name="claude-haiku-4-5-20251001",
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
    
    # ===== CloudGPT AsyncAzureOpenAI client =====
    async_openai_client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )
    
    adapter = TraceToMessages()

    # create APO instance
    # ===== Original APO config (for 5 instances) =====
    # apo = APO[Dict[str, Any]](
    #     async_openai_client=async_openai_client,
    #     gradient_model="gpt-4o-20241120",
    #     apply_edit_model="gpt-4o-20240806",
    #     diversity_temperature=0.7,
    #     gradient_batch_size=3,
    #     val_batch_size=2,
    #     beam_width=2,
    #     branch_factor=2,
    #     beam_rounds=3,
    #     rollout_batch_timeout=3600.0,
    #     _poml_trace=True
    # )
    
    # ===== Single instance APO config =====
    # apo = APO[Dict[str, Any]](
    #     async_openai_client=async_openai_client,
    #     gradient_model="gpt-5-20250807",
    #     apply_edit_model="gpt-5-20250807",
    #     diversity_temperature=1.0,
    #     gradient_batch_size=1,  # only 1 training sample
    #     val_batch_size=1,       # 1 validation instance per evaluation
    #     beam_width=1,           # single beam for testing
    #     branch_factor=1,        # single branch
    #     beam_rounds=2,          # 2 rounds to see APO effect (round1: seed, round2: optimized)
    #     rollout_batch_timeout=3600.0,
    #     _poml_trace=True
    # )
    
    # ===== Single-instance APO config =====
    apo = APO[Dict[str, Any]](
        async_openai_client=async_openai_client,
        gradient_model="gpt-5-20250807",
        apply_edit_model="gpt-5-20250807",
        diversity_temperature=1.0,
        gradient_batch_size=1,  
        val_batch_size=1,
        beam_width=1,           # single beam for single instance
        branch_factor=1,
        beam_rounds=2,
        rollout_batch_timeout=3600.0,
        _poml_trace=True
    )
    apo.set_store(store)
    
    apo.set_initial_resources({
        "llm": llm_proxy.as_resource(model="local"),
        "prompt_template": PromptTemplate(template=SEED_PROMPT, engine="f-string")
    })
    apo.set_adapter(adapter)

    # ===== Original dataset (5 instances) =====
    # dataset = load_dataset("temp_instance_astropy__astropy-13398.jsonl")  
    # train, val = split_dataset(dataset, train_size=3, val_size=2)
    
    # ===== Single instance for testing =====
    # dataset = load_dataset("temp_instance_astropy__astropy-12907.jsonl")  # astropy__astropy-13033
    # train, val = split_dataset(dataset, train_size=1, val_size=0)
    
    # ===== Single instance + separate validation set =====
    # train_dataset = load_dataset("temp_instance_astropy__astropy-12907.jsonl")
    # val_dataset = load_dataset("temp_instance_astropy__astropy-13453.jsonl")
    # train = train_dataset[:1]  # Use only 1 instance for gradient computation
    # val = val_dataset  # Use all instances for validation
    
    # ===== astropy__astropy-7606.jsonl for both train and val =====
    dataset = load_dataset("astropy__astropy-7606.jsonl")
    train, val = dataset, dataset
    
    # run apo algorithm
    await apo.run(
        train_dataset=train,
        val_dataset=val,
    )
        

async def main():
    store = agl.LightningStoreClient("http://localhost:4748")
    try:
        await apo_algorithm(store=store)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
