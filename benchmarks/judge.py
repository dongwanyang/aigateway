"""LLM-as-judge quality scoring module.

Design decisions:
- Judge model bypasses the被测 gateway (direct provider call) to avoid cache/compression/RAG contamination.
- temperature=0 for determinism.
- Double judging (2 independent calls) averaged to reduce variance.
- Graceful degradation: API timeout / invalid JSON -> sample.status="judge_error", quality_score=null.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

JUDGE_PROMPT_TEMPLATE = """You are an impartial judge evaluating two AI responses to the same user prompt.

User prompt:
{prompt}

Response A:
{response_a}

Response B:
{response_b}

Rate each response on a scale of 1-5 based on:
1. Helpfulness: Does it directly answer the question?
2. Accuracy: Is the information correct and well-reasoned?
3. Clarity: Is it well-structured and easy to understand?

Rules:
- Score independently for A and B.
- Do not prefer one based on length; prefer substance.
- If either response contains errors or hallucinations, penalize heavily.
- Return ONLY valid JSON: {{"response_a_score": <int>, "response_b_score": <int>}}

Scores:
"""


async def run_judge(
    prompt: str,
    response_a: str,
    response_b: str,
    judge_api_url: str,
    judge_api_key: str,
    judge_model: str = "deepseek-v4-flash",
    max_retries: int = 2,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[float]:
    """Run LLM-as-judge between two responses. Returns average of 2 runs, or None on error."""
    if not judge_api_url or not judge_api_key:
        logger.warning("Judge API credentials missing, skipping quality scoring")
        return None

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        scores = []
        for attempt in range(2):
            score = await _single_judge(
                prompt=prompt,
                response_a=response_a,
                response_b=response_b,
                api_url=judge_api_url,
                api_key=judge_api_key,
                model=judge_model,
                max_retries=max_retries,
                shuffle=(attempt == 1),
                session=session,
            )
            if score is not None:
                scores.append(score)
            else:
                logger.warning(f"Judge attempt {attempt + 1} failed")

        return sum(scores) / len(scores) if scores else None
    finally:
        if close_session:
            await session.close()


async def _single_judge(
    prompt: str,
    response_a: str,
    response_b: str,
    api_url: str,
    api_key: str,
    model: str,
    max_retries: int,
    shuffle: bool = False,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[float]:
    """Execute one judging call with retries."""
    if shuffle:
        response_a, response_b = response_b, response_a

    user_message = JUDGE_PROMPT_TEMPLATE.format(
        prompt=_truncate(prompt, 2000),
        response_a=_truncate(response_a, 3000),
        response_b=_truncate(response_b, 3000),
    )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": user_message}],
        "temperature": 0,
        "max_tokens": 100,
    }

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            async with session.post(
                api_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    last_error = f"HTTP {resp.status}: {text[:200]}"
                    logger.warning(f"Judge API error (attempt {attempt + 1}): {last_error}")
                    if attempt < max_retries:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return None

                body = await resp.json()
                content = body.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

                parsed = _parse_judge_response(content)
                if parsed is None:
                    last_error = f"Invalid JSON in judge response: {content[:200]}"
                    logger.warning(f"Judge parse error (attempt {attempt + 1}): {last_error}")
                    if attempt < max_retries:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return None

                return parsed

        except asyncio.TimeoutError:
            last_error = "Judge API timeout"
            logger.warning(f"Judge timeout (attempt {attempt + 1})")
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
                continue
            return None
        except Exception as e:
            last_error = str(e)[:200]
            logger.warning(f"Judge exception (attempt {attempt + 1}): {last_error}")
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
                continue
            return None

    logger.error(f"Judge failed after {max_retries + 1} attempts: {last_error}")
    return None


def _parse_judge_response(content: str) -> Optional[float]:
    """Parse judge JSON response and return average score."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        else:
            return None

    score_a = data.get("response_a_score")
    score_b = data.get("response_b_score")

    if score_a is None or score_b is None:
        return None

    try:
        score_a = float(score_a)
        score_b = float(score_b)
    except (TypeError, ValueError):
        return None

    # Clamp to 1-5
    score_a = max(1.0, min(5.0, score_a))
    score_b = max(1.0, min(5.0, score_b))

    return (score_a + score_b) / 2.0


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len characters."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."
