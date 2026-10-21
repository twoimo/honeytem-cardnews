"""Deterministic Korean copy. Never synthesize product claims or issued links."""
from __future__ import annotations

from .schema import Job

PROGRAM_LABELS = {
    "toss_sharelink": "토스 쉐어링크",
    "coupang_partners": "쿠팡 파트너스",
    "kakao_gift_curator": "카카오톡 선물하기 큐레이터",
}


def caption(job: Job) -> str:
    m = job.manifest
    affiliate = m["mode"] == "affiliate"
    if affiliate:
        lines = [d["text"] for d in m["disclosures"]]
    else:
        lines = ["합성 테스트 이미지 · 판매용 아님" if m["fixture"] else "콘셉트 샘플 · AI 이미지"]
    lines += ["", m["title"], m["subtitle"], ""]
    for p in m["products"]:
        lines += [f'{p["id"]}. {p["title"]}', p["comment"]]
        if affiliate:
            price = p["price"]
            lines += [f'{p["seller"]} · {p["option"]} · {p["quantity"]}개',
                      f'{price["amount"]:,}원 · {price["conditions"]}',
                      f'배송: {price["shipping"]}',
                      f'상품 정보 확인: {p["evidence"]["checked_at"]}']
        lines.append("")
    if affiliate:
        if m["channel"]["profile_links_confirmed"]:
            lines += ["제품 정보는 프로필 링크에서 같은 번호로 확인해 주세요."]
        else:
            lines += ["상품별 원본 링크는 별도 링크 목록에서 같은 번호로 확인해 주세요."]
        lines += ["구매 시점의 상품 페이지에서 최종 가격·옵션·배송 조건을 확인해 주세요."]
    else:
        lines += ["실제 판매 상품을 소개하지 않습니다. 가격과 구매 링크는 제공하지 않습니다."]
    return "\n".join(lines) + "\n"


def links(job: Job) -> list[dict]:
    """Exact input Unicode URL strings, no stripping/query sorting/rewriting."""
    return [{"id": p["id"], "title": p["title"], "program": p["program"],
             "program_label": PROGRAM_LABELS.get(p["program"]),
             "option": p["option"], "original_url": p["original_url"],
             "affiliate_url": p["affiliate_url"],
             "checked_at": p["evidence"]["checked_at"] if p["evidence"] else None}
            for p in job.manifest["products"]]
