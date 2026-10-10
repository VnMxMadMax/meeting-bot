"""The one model call Otto makes, and its token accounting.

Mirrors main._invoke_pass: retry, strip code fences, parse, check the keys the caller
requires. A truncated response is reported rather than parsed into something plausible,
because a half-read extraction looks exactly like a short meeting.
"""

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Tuple

from .config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TEMPERATURE, logger
from .prompt import OTTO_MEETING_TASKS_HUMAN, OTTO_MEETING_TASKS_SYSTEM


def build_chain():
    """prompt | llm — the same chain shared.py registers as `otto_meeting_tasks`."""
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI

    prompt = ChatPromptTemplate.from_messages([
        ("system", OTTO_MEETING_TASKS_SYSTEM),
        ("human", OTTO_MEETING_TASKS_HUMAN),
    ])
    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=OPENAI_TEMPERATURE,
                     openai_api_key=OPENAI_API_KEY)
    return prompt | llm


def extract_llm_meta(response):
    """content + finish_reason + token counts, however the SDK reported them."""
    content = getattr(response, "content", None) or ""
    meta = getattr(response, "response_metadata", {}) or {}
    usage = getattr(response, "usage_metadata", None) or meta.get("token_usage", {}) or {}
    finish = meta.get("finish_reason")
    ptok = usage.get("input_tokens", usage.get("prompt_tokens"))
    ctok = usage.get("output_tokens", usage.get("completion_tokens"))
    rtok = (usage.get("output_token_details") or {}).get("reasoning")
    return content, finish, ptok, ctok, rtok


async def invoke_extraction(prompt_inputs: Dict[str, Any], user_id: str,
                            max_retries: int = 3
                            ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[str]]:
    """One guarded extraction pass. Returns (extraction, usage, error)."""
    if not OPENAI_API_KEY:
        return None, {"pass": "otto_meeting_tasks"}, "OPENAI_API_KEY is not set"

    chain = build_chain()
    loop = asyncio.get_event_loop()

    for attempt in range(1, max_retries + 1):
        try:
            response = await loop.run_in_executor(None, lambda: chain.invoke(prompt_inputs))
            content, finish, ptok, ctok, rtok = extract_llm_meta(response)
            usage = {"pass": "otto_meeting_tasks", "finish_reason": finish,
                     "prompt": ptok, "completion": ctok, "reasoning": rtok,
                     "total": (ptok or 0) + (ctok or 0)}
            logger.info("[LLM] otto_meeting_tasks user=%s attempt=%d finish=%s total=%s",
                        user_id, attempt, finish, usage["total"])

            if not content:
                await asyncio.sleep(1)
                continue
            if finish == "length":
                # a truncated extraction is not a short meeting — say so
                return None, usage, "extraction truncated (finish_reason=length)"

            c = content.strip()
            if c.startswith("```json"):
                c = c[7:].strip()
            if c.startswith("```"):
                c = c[3:].strip()
            if c.endswith("```"):
                c = c[:-3].strip()
            data = json.loads(c)

            if "tasks" not in data:
                logger.error("otto extraction missing `tasks`")
                continue
            return data, usage, None

        except json.JSONDecodeError as e:
            logger.error("otto extraction JSON parse failed attempt=%d: %s", attempt, e)
            continue
        except Exception as e:
            logger.error("otto extraction failed attempt=%d: %s", attempt, e)
            if attempt == max_retries:
                return None, {"pass": "otto_meeting_tasks"}, str(e)
            await asyncio.sleep(1)

    return None, {"pass": "otto_meeting_tasks"}, \
        "extraction failed after %d retries" % max_retries


def summarize_usages(usages):
    """Verbatim from main._summarize_usages."""
    return {
        "llm_calls":         len(usages),
        "prompt_tokens":     sum((u.get("prompt") or 0) for u in usages),
        "completion_tokens": sum((u.get("completion") or 0) for u in usages),
        "reasoning_tokens":  sum((u.get("reasoning") or 0) for u in usages),
        "total_tokens":      sum((u.get("total") or 0) for u in usages),
        "per_pass":          usages,
    }
