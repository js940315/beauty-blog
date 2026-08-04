"""Claude 호출 + 자동 수리 루프.

본문 생성은 한 번에 끝나지 않는다.
validator가 실제로 세서 위반을 되먹이고, 최대 REPAIR_ROUNDS 회 재작성한다.
그래도 안 맞으면 polish()가 기계적으로 강제 복구한다.
"""

import json

import anthropic

import brief
import config as C
import prompts
import validator

_client = None


class RefusalError(RuntimeError):
    """안전 분류기가 요청을 거절했을 때."""


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _text_of(message) -> str:
    parts = [b.text for b in message.content if b.type == "text"]
    return "\n".join(parts).strip()


def _call(system, user, max_tokens, schema=None):
    """Messages API 한 번 호출. max_tokens가 크면 스트리밍으로 간다.

    Opus 5는 thinking이 기본 ON이고 max_tokens가 사고+본문을 함께 덮는다.
    그래서 본문 생성은 여유를 크게 잡고 스트리밍으로 받는다.
    temperature/top_p/top_k는 Opus 5에서 400이므로 쓰지 않는다.
    """
    output_config = {"effort": C.EFFORT}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}

    kwargs = dict(
        model=C.MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config=output_config,
    )

    if max_tokens > 16000:
        with client().messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
    else:
        msg = client().messages.create(**kwargs)

    if msg.stop_reason == "refusal":
        cat = getattr(getattr(msg, "stop_details", None), "category", None)
        raise RefusalError(f"안전 분류기 거절 (category={cat})")
    if msg.stop_reason == "max_tokens":
        print(f"  [경고] max_tokens({max_tokens})에 걸려 잘렸습니다. 값을 올리세요.")

    return _text_of(msg)


# ── 1. 팩트시트 ─────────────────────────────────────────────────────────

def build_factsheet(celeb: str, sources: str) -> dict:
    raw = _call(
        prompts.FACTSHEET_SYSTEM,
        prompts.FACTSHEET_USER.format(celeb=celeb, sources=sources),
        C.MAX_TOKENS_FACT,
        schema=prompts.FACTSHEET_SCHEMA,
    )
    return json.loads(raw)


# 팩트시트 정리는 brief.py 에 있다. SDK 없이도 써야 하는 함수라서 거기 뒀다.
factsheet_text = brief.factsheet_text


# ── 2. 제목 ─────────────────────────────────────────────────────────────

def generate_titles(fs_text: str):
    raw = _call(
        prompts.TITLE_SYSTEM,
        prompts.TITLE_USER.format(factsheet=fs_text),
        C.MAX_TOKENS_TITLE,
        schema=prompts.TITLE_SCHEMA,
    )
    return json.loads(raw).get("titles", [])


# ── 3. 본문 + 수리 루프 ─────────────────────────────────────────────────

def generate_body(title, fs_text, richness, cta, has_exercise, celeb, heart):
    user = prompts.body_user(title, fs_text, richness, cta, has_exercise, celeb, heart)
    body = _call(prompts.BODY_SYSTEM, user, C.MAX_TOKENS_BODY)

    history = []
    for attempt in range(1, C.REPAIR_ROUNDS + 1):
        problems = validator.validate(body, richness, cta, celeb, title)
        history.append({"attempt": attempt, "problems": problems})
        if not problems:
            return body, history, False

        print(f"  [수리 {attempt}/{C.REPAIR_ROUNDS}] 위반 {len(problems)}건")
        for p in problems[:5]:
            print(f"      · {p}")

        body = _call(
            prompts.BODY_SYSTEM,
            prompts.REPAIR_USER.format(
                problems="\n".join(f"- {p}" for p in problems),
                previous=body,
            ),
            C.MAX_TOKENS_BODY,
        )

    # 재작성으로도 안 되면 기계적 강제 복구
    final = validator.polish(body)
    remaining = validator.validate(final, richness, cta, celeb, title)
    history.append({"attempt": "polish", "problems": remaining})
    if remaining:
        print(f"  [polish] 강제 복구 후에도 {len(remaining)}건 남음")
        for p in remaining[:5]:
            print(f"      · {p}")
    return final, history, True
