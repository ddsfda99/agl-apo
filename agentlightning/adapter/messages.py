# Copyright (c) Microsoft. All rights reserved.

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, Generator, Iterable, List, Optional, TypedDict, Union, cast

from pydantic import TypeAdapter

logger = logging.getLogger(__name__)

from agentlightning.types import Span

from .base import TraceAdapter

if TYPE_CHECKING:
    from openai.types.chat import (
        ChatCompletionFunctionToolParam,
        ChatCompletionMessageFunctionToolCallParam,
        ChatCompletionMessageParam,
    )


class OpenAIMessages(TypedDict):
    """OpenAI-style chat messages with optional tool definitions.

    Attributes:
        messages: Ordered chat messages that describe the conversation.
        tools: Tool specifications available to the assistant, if any.
    """

    messages: List[ChatCompletionMessageParam]
    tools: Optional[List[ChatCompletionFunctionToolParam]]


class _RawSpanInfo(TypedDict):
    """Intermediate representation parsed from a span.

    Attributes:
        prompt: Prompt messages reconstructed from span attributes.
        completion: Assistant completions following tool invocations.
        request: Request payload recorded in the trace.
        response: Response payload recorded in the trace.
        tools: Tool call metadata extracted from child spans.
    """

    prompt: List[Dict[str, Any]]
    completion: List[Dict[str, Any]]
    request: Dict[str, Any]
    response: Dict[str, Any]
    tools: List[Dict[str, Any]]


def group_genai_dict(data: Dict[str, Any], prefix: str) -> Union[Dict[str, Any], List[Any]]:
    """Convert flattened trace attributes into nested structures.

    Attributes emitted by the tracing pipeline often arrive as dotted paths (for example
    `gen_ai.prompt.0.role`). This helper groups those keys into nested dictionaries or lists so that
    downstream processing can operate on structured data.

    Args:
        data: Flat dictionary whose keys are dotted paths.
        prefix: Top-level key (for example `gen_ai.prompt`) that determines which attributes are
            grouped.

    Returns:
        A nested dictionary (no numeric index detected) or list (numeric indices detected) containing
        the grouped values.
    """
    result: Union[Dict[str, Any], List[Any]] = {}

    # Collect keys that match the prefix
    relevant = {k[len(prefix) + 1 :]: v for k, v in data.items() if k.startswith(prefix + ".")}

    # Detect if we have numeric indices (-> list) or not (-> dict)
    indexed = any(part.split(".")[0].isdigit() for part in relevant.keys())

    if indexed:
        # Group by index
        grouped: Dict[int, Dict[str, Any]] = defaultdict(dict)
        for k, v in relevant.items():
            parts = k.split(".")
            if not parts[0].isdigit():
                continue
            idx, rest = int(parts[0]), ".".join(parts[1:])
            grouped[idx][rest] = v
        # Recursively build
        result = []
        for i in sorted(grouped.keys()):
            result.append(group_genai_dict({f"{prefix}.{rest}": val for rest, val in grouped[i].items()}, prefix))
    else:
        # No indices: build dict
        nested: Dict[str, Any] = defaultdict(dict)
        for k, v in relevant.items():
            if "." in k:
                head, _tail = k.split(".", 1)
                nested[head][f"{prefix}.{k}"] = v
            else:
                result[k] = v
        # Recurse into nested dicts
        for head, subdict in nested.items():
            result[head] = group_genai_dict(subdict, prefix + "." + head)

    return result


def convert_to_openai_messages(prompt_completion_list: List[_RawSpanInfo]) -> Generator[OpenAIMessages, None, None]:
    """Convert raw trace payloads into OpenAI-style chat messages.

    The function consumes an iterable produced by
    [`TraceToMessages.adapt()`][agentlightning.TraceToMessages.adapt] and yields
    structures that match the OpenAI fine-tuning JSONL schema, including tool definitions.

    Args:
        prompt_completion_list: Raw prompt/completion/tool payloads extracted from a trace.

    Returns:
        A generator that yields [`OpenAIMessages`][agentlightning.adapter.messages.OpenAIMessages]
        entries compatible with the OpenAI Functions fine-tuning format.
    """

    # Import locally to avoid legacy OpenAI version type import errors
    from openai.types.chat import (
        ChatCompletionAssistantMessageParam,
        ChatCompletionFunctionToolParam,
        ChatCompletionMessageFunctionToolCallParam,
        ChatCompletionMessageParam,
    )

    for pc_entry in prompt_completion_list:
        messages: List[ChatCompletionMessageParam] = []

        # Extract messages
        for msg in pc_entry["prompt"]:
            role = msg["role"]

            if role == "assistant" and "tool_calls" in msg:
                # Use the tool_calls directly
                # This branch is usually not used in the wild.
                tool_calls: List[ChatCompletionMessageFunctionToolCallParam] = [
                    ChatCompletionMessageFunctionToolCallParam(
                        id=call["id"],
                        type="function",
                        function={"name": call["name"], "arguments": call["arguments"]},
                    )
                    for call in msg["tool_calls"]
                ]
                messages.append(
                    ChatCompletionAssistantMessageParam(role="assistant", content=None, tool_calls=tool_calls)
                )
            else:
                # Normal user/system/tool content
                message = cast(
                    ChatCompletionMessageParam,
                    TypeAdapter(ChatCompletionMessageParam).validate_python(
                        dict(role=role, content=msg.get("content", ""), tool_call_id=msg.get("tool_call_id", None))
                    ),
                )
                messages.append(message)

        # Extract completions (assistant outputs after tool responses)
        for comp in pc_entry["completion"]:
            if comp.get("role") == "assistant":
                content = comp.get("content")
                if pc_entry["tools"]:
                    tool_calls = [
                        ChatCompletionMessageFunctionToolCallParam(
                            id=tool["call"]["id"],
                            type=tool["call"]["type"],
                            function={"name": tool["name"], "arguments": tool["parameters"]},
                        )
                        for tool in pc_entry["tools"]
                    ]
                    messages.append(
                        ChatCompletionAssistantMessageParam(role="assistant", content=content, tool_calls=tool_calls)
                    )
                else:
                    messages.append(ChatCompletionAssistantMessageParam(role="assistant", content=content))

        # Build tools definitions (if available)
        if "functions" in pc_entry["request"]:
            tools = [
                ChatCompletionFunctionToolParam(
                    type="function",
                    function={
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "parameters": (
                            json.loads(fn["parameters"]) if isinstance(fn["parameters"], str) else fn["parameters"]
                        ),
                    },
                )
                for fn in pc_entry["request"]["functions"]
            ]
            yield OpenAIMessages(messages=messages, tools=tools)
        else:
            yield OpenAIMessages(messages=messages, tools=None)


class TraceToMessages(TraceAdapter[List[OpenAIMessages]]):
    """Convert trace spans into OpenAI-compatible conversation messages.

    The adapter reconstructs prompts, completions, tool calls, and function definitions from
    `gen_ai.*` span attributes. The resulting objects match the JSONL structure expected by the
    OpenAI fine-tuning pipeline.

    !!! warning
        The adapter assumes all spans share a common trace and that tool call spans are direct
        children of the associated completion span.
    """

    def get_tool_calls(self, completion: Span, all_spans: List[Span], /) -> Iterable[Dict[str, Any]]:
        """Yield tool call payloads for a completion span.

        Args:
            completion: The completion span whose descendants should be inspected.
            all_spans: The complete span list belonging to the trace.

        Yields:
            Dictionaries describing tool calls with identifiers, names, and arguments.

        Raises:
            ValueError: If a candidate tool span cannot be converted into a dictionary.
        """
        # Get all the spans that are children of the completion span
        children = [span for span in all_spans if span.parent_id == completion.span_id]
        # Get the tool calls from the children
        for maybe_tool_call in children:
            tool_call = group_genai_dict(maybe_tool_call.attributes, "tool")
            if not isinstance(tool_call, dict):
                raise ValueError(f"Extracted tool call from trace is not a dict: {tool_call}")
            if tool_call:
                yield tool_call

    def adapt(self, source: List[Span], /) -> List[OpenAIMessages]:
        """Transform trace spans into OpenAI chat payloads.

        Args:
            source: Spans containing `gen_ai.*` attributes emitted by the tracing pipeline.

        Returns:
            A list of [`OpenAIMessages`][agentlightning.adapter.messages.OpenAIMessages] entries that
            capture prompts, completions, tools, and metadata.
        """
        raw_prompt_completions: List[_RawSpanInfo] = []

        for span in source:
            attributes = {k: v for k, v in span.attributes.items()}

            # Get all related information from the trace span
            prompt = group_genai_dict(attributes, "gen_ai.prompt") or []
            completion = group_genai_dict(attributes, "gen_ai.completion") or []
            request = group_genai_dict(attributes, "gen_ai.request") or {}
            response = group_genai_dict(attributes, "gen_ai.response") or {}
            if not isinstance(prompt, list):
                raise ValueError(f"Extracted prompt from trace is not a list: {prompt}")
            if not isinstance(completion, list):
                raise ValueError(f"Extracted completion from trace is not a list: {completion}")
            if not isinstance(request, dict):
                raise ValueError(f"Extracted request from trace is not a dict: {request}")
            if not isinstance(response, dict):
                raise ValueError(f"Extracted response from trace is not a dict: {response}")
            if prompt or completion or request or response:
                tools = list(self.get_tool_calls(span, source)) or []
                raw_prompt_completions.append(
                    _RawSpanInfo(
                        prompt=prompt or [], completion=completion, request=request, response=response, tools=tools
                    )
                )

        return list(convert_to_openai_messages(raw_prompt_completions))

    def extract_trace(self, span: Span) -> List[Dict[str, Any]]:
        """
        从单个span的attributes中提取gen_ai.prompt信息。
        
        Args:
            span: 包含gen_ai.prompt.*属性的Span对象
            
        Returns:
            按顺序排列的消息列表 [{"role": "...", "content": "..."}]
        """
        res: Dict[str, Dict[str, Any]] = {}
        attributes = span.attributes or {}
        
        for k, v in attributes.items():
            if "gen_ai.prompt" in k:
                # 尝试JSON解析（处理字符串形式的数据）
                try:
                    v_eval = json.loads(v) if isinstance(v, str) else v
                except (json.JSONDecodeError, TypeError):
                    v_eval = v
                
                # 提取索引：gen_ai.prompt.2.role -> parts=['gen_ai', 'prompt', '2', 'role']
                parts = k.split(".")
                if len(parts) >= 3 and parts[2].isdigit():
                    index = parts[2]
                    
                    if index not in res:
                        res[index] = {}
                    
                    # 检查是content还是role
                    if "content" in k:
                        res[index]["content"] = v_eval
                    elif "role" in k:
                        res[index]["role"] = v_eval
        
        # 按索引排序构建trace
        trace: List[Dict[str, Any]] = []
        for idx in sorted(res.keys(), key=lambda x: int(x)):
            if "role" in res[idx] and "content" in res[idx]:
                trace.append(res[idx])
        
        return trace

    def find_span_with_max_prompt_index(self, spans: List[Span]) -> Optional[Span]:
        """
        找到包含最大gen_ai.prompt索引的span。
    
        这个span包含最完整的对话历史。
    
        Args:
            spans: Span列表
        
        Returns:
            包含最大prompt索引的span
        """
        max_prompt_index = -1
        max_span = None
        
        logger.info(f"[TraceToMessages] Finding span with max prompt index from {len(spans)} spans")
    
        for i, span in enumerate(spans):
            attributes = span.attributes or {}
            current_max = -1
            
            # 找到这个span中最大的prompt索引
            for k in attributes.keys():
                if k.startswith("gen_ai.prompt"):
                    parts = k.split(".")
                    if len(parts) >= 3 and parts[2].isdigit():
                        idx = int(parts[2])
                        if idx > current_max:
                            current_max = idx

            logger.debug(f"[TraceToMessages] Span {i} (sequence_id={span.sequence_id}): max_prompt_index={current_max}")
            
            # 如果这个span的最大prompt索引更大，记录它
            if current_max > max_prompt_index:
                max_prompt_index = current_max
                max_span = span
        
        if max_span:
            logger.info(f"[TraceToMessages] Selected span with max_prompt_index={max_prompt_index}, sequence_id={max_span.sequence_id}")
        else:
            logger.warning(f"[TraceToMessages] No span with gen_ai.prompt found!")
        
        return max_span

    def extract_longest_trace(self, spans: List[Span]) -> List[Dict[str, Any]]:
        """
        从spans中找到prompt索引最大的span，提取其完整trace。
        
        Args:
            spans: Span列表
            
        Returns:
            最完整的trace: [{"role": "...", "content": "..."}]
        """
        max_span = self.find_span_with_max_prompt_index(spans)
        
        if max_span:
            trace = self.extract_trace(max_span)
            logger.info(f"[TraceToMessages] Extracted trace with {len(trace)} messages")
            
            # 打印trace的前2条和最后1条（用于调试）
            if trace:
                logger.debug(f"[TraceToMessages] First message: role={trace[0].get('role')}, content_length={len(str(trace[0].get('content', '')))}")
                if len(trace) > 1:
                    logger.debug(f"[TraceToMessages] Last message: role={trace[-1].get('role')}, content_length={len(str(trace[-1].get('content', '')))}")
            
            return trace
    
        logger.warning(f"[TraceToMessages] No max_span found, returning empty trace")
        return []


